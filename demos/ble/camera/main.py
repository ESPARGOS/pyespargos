#!/usr/bin/env python
"""BLE advertiser directions over a live camera image."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
import numpy as np
from PyQt6 import QtCore

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from demos.common.application import CombinedArrayMixin
from demos.ble.common.application import ESPARGOSBluetoothApplication
from demos.common.camera.view import CameraView
from demos.common.camera.video import CameraUnavailableError
from demos.common.camera.overlay import CameraOverlayModel
from demos.ble.common.controller import BLEController
from espargos.ble_beamforming import BLEBeamformer
from demos.ble.common.beamforming import combine_board_spectra, processing_config, render_spectrum, render_polarization, measurement_opacity, spectrum_intensity
from espargos.array_processing import steering_vectors_2d
from espargos.ble_packet import channel_frequency
from espargos.ble_receiver import BLECalibrationError
from espargos.polarization import polarization_ellipse


class BLEOverlay(CameraOverlayModel):
    polarization_methods = ("Bartlett", "MUSIC", "Root-MUSIC")

    def publish_spatial_spectrum(self, power):
        self.publish_spatial_intensity(spectrum_intensity(power))

    def publish_spatial_intensity(self, intensity):
        image = np.zeros((*intensity.T.shape, 4), np.uint8)
        image[..., 1] = np.clip(intensity.T * 255, 0, 255).astype(np.uint8)
        image[..., 3] = 255
        self.publish_image("spatial", image)


class BLECamera(CombinedArrayMixin, ESPARGOSBluetoothApplication):
    QML_FILE = Path(__file__).with_name("Main.qml")
    spectrumArrived = QtCore.pyqtSignal(object, object, float)
    stateChanged = QtCore.pyqtSignal()
    beamformingError = QtCore.pyqtSignal(int, str)
    DEFAULT_CONFIG = {
        "camera": CameraView.DEFAULT_CONFIG,
        "receiver": dict(BLEController.DEFAULT_CONFIG, advertisers=[]),
        "beamformer": {
            "type": "Bartlett",
            "backlog_size": 8,
            "backlog_age": 0.5,
            "resolution_azimuth": 128,
            "resolution_elevation": 96,
            "polarization_mode": "ignore",
            "multipath": True,
            "polarization_speed": 2.0,
            "normalize_polarization": False,
            "grid_spacing": 24,
            "colorize_delay": False,
        },
        "visualization": {
            "space": "camera",
            "overlay": "Abs",
            "manual_exposure": False,
            "azimuth_correction": 0.0,
            "elevation_correction": 0.0,
            "max_age": 1.0,
            "edge_markers": True,
        },
    }

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        parser.add_argument("--no-camera", action="store_true", help="Show the radio overlay without a video camera")

    def _process_args(self):
        super()._process_args()
        settings = self.initial_config["app"]
        if len(self.get_initial_config("combined-array", "boards", default={})) > 1:
            # Gain changes within a packet can leave too few same-feed pairs
            # to locate it. Use a common fixed gain on a combined aperture.
            explicit = self.get_explicit_initial_config("app", "receiver", default={})
            for key, value in (("gain_mode", "manual"), ("gain", 45)):
                if key not in explicit:
                    settings["receiver"][key] = value
        if settings["beamformer"]["polarization_mode"] != "ignore":
            settings["receiver"]["polarization"] = 4

    def __init__(self, argv):
        CameraView.configure_rendering()
        parser = argparse.ArgumentParser(description="ESPARGOS BLE camera", add_help=False)
        super().__init__(argv, argparse_parent=parser)

    def _initialize_display(self):
        if self.args.no_camera:
            self.appconfig.set({"camera": {"enable": False}})
        self._spectra = {}
        self._polarizations = {}
        self._polarization_field = None
        self._beamformers = {}
        self._measurement_after = time.monotonic()
        self._phase_errors = set()
        self.controller.stateChanged.connect(self.stateChanged)
        self._selected = frozenset()
        self._beamformer_epoch = self._measurement_after
        self.spectrumArrived.connect(self._spectrum)
        self.beamformingError.connect(self._receiver_error)
        self.camera_view = CameraView(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        self.video_camera = self.camera_view.initialize()
        self.overlay = BLEOverlay(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        self.overlay.attach_to_engine(self.engine)
        self._processing_signature = None
        self.appConfigChanged.connect(self._processing_config_changed)
        self.appconfig.updateUIState.connect(self._processing_config_changed)
        return {"CameraView": self.camera_view, "WebCam": self.video_camera, "overlayModel": self.overlay}

    def _clear(self):
        self._measurement_after = time.monotonic()
        self._worker.clear()
        self._spectra.clear()
        self._polarizations.clear()
        self._polarization_field = None
        self._phase_errors = set()

    def _receiver_config_changed(self, config):
        super()._receiver_config_changed(config)
        self._processing_config_changed()

    def _processing_config_changed(self):
        config = self.appconfig.get("receiver")
        signature = (processing_config(self.appconfig.get("beamformer")), config["channel"], self.overlay.resolutionAzimuth, self.overlay.resolutionElevation)
        if signature == self._processing_signature:
            return
        self._processing_signature = signature
        self._clear()
        if hasattr(self, "indexing_matrix"):
            layouts = {}
            for b in range(len(self.pool.hosts)):
                rows, cols = np.nonzero(self.indexing_matrix // 8 == b)
                region = np.s_[rows.min() : rows.max() + 1, cols.min() : cols.max() + 1]
                order = self.indexing_matrix[region] % 8
                steering = steering_vectors_2d(*order.shape, signature[2], signature[3], frequency_hz=channel_frequency(config["channel"]), pixel_centers=True)
                layouts[b] = order, self.antenna_orientations[region], steering
            self._board_layouts = layouts

    def _connected(self):
        self._processing_signature = None
        self._receiver_config_changed(self.controller.receiverConfig)
        super()._connected()

    def _selection_changed(self):
        self._selected = frozenset(self.advertisers.selected)

    def _measure(self, cluster):
        packet = cluster.packet
        identity = packet.address_type + ":" + (packet.advertiser or "")
        if self.controller.busy or cluster.is_calibration or identity not in self._selected or not self.controller.calibrationSupported:
            return
        if not packet.is_advertisement or np.count_nonzero(np.isfinite(cluster.rx_gain)) < 2:
            return
        self._worker.submit((identity, cluster.board_index), (cluster, time.monotonic()), keep=int(self.appconfig.get("beamformer", "backlog_size")))

    def _process(self, item):
        cluster, received = item
        if self._beamformer_epoch != self._measurement_after:
            self._beamformers.clear()
            self._beamformer_epoch = self._measurement_after
        packet = cluster.packet
        identity = packet.address_type + ":" + (packet.advertiser or "")
        config = processing_config(self.appconfig.get("beamformer"))
        if self.controller.busy or identity not in self._selected or received < self._measurement_after or time.monotonic() - received >= config.history_age:
            return
        b = cluster.board_index
        try:
            iq = self.receiver.packet_iq(cluster)
            phase = iq.phase()
        except BLECalibrationError as error:
            if self.controller.busy or received < self._measurement_after:
                return
            if b not in self._phase_errors:
                self._phase_errors.add(b)
                self._beamformers.pop(b, None)
                self.beamformingError.emit(b, str(error))
            return
        self._phase_errors.discard(b)
        if np.count_nonzero(phase.valid[b]) >= 2:
            # An operation may have completed while this observation was fitted.
            if received < self._measurement_after:
                return
            order, orientations, steering = self._board_layouts[b]
            covariance, sample_counts = iq.covariance()
            if b not in self._beamformers:
                self._beamformers[b] = BLEBeamformer()
            result = self._beamformers[b].process(
                identity,
                phase.vector[b].ravel()[order],
                cluster.rf_switch_state[b].ravel()[order],
                received,
                steering,
                channel_frequency(packet.channel),
                config,
                amplitude=phase.amplitude[b].ravel()[order] if phase.amplitude is not None else None,
                orientations=orientations,
                covariance=covariance[np.ix_(order.ravel(), order.ravel())],
                sample_counts=sample_counts[np.ix_(order.ravel(), order.ravel())],
            )
            power = render_spectrum(result, steering.shape[2:])
            directions, jones = result.directions.tolist(), render_polarization(result, steering.shape[2:])
            # A sparse random-feed window may temporarily lack coverage. Keep
            # the last usable frame until its normal age limit expires.
            if np.any(power > 0):
                self.spectrumArrived.emit((identity, b), (power * float(np.min(phase.coherence[b][phase.valid[b]])), directions, jones), received)

    @QtCore.pyqtSlot(object, object, float)
    def _spectrum(self, identity, result, received):
        if self.controller.busy or identity[0] not in self.advertisers.selected or received < self._measurement_after:
            return
        if time.monotonic() - received >= float(self.appconfig.get("visualization", "max_age")):
            return
        power, directions, jones = result
        if power.shape != (self.overlay.resolutionAzimuth, self.overlay.resolutionElevation):
            return
        self._spectra[identity] = (received, power, directions)
        if jones is not None:
            self._polarizations[identity] = (received, jones)

    @QtCore.pyqtSlot(int, str)
    def _receiver_error(self, board, error):
        for key in [key for key in self._spectra if key[1] == board]:
            del self._spectra[key]
            self._polarizations.pop(key, None)
        self.controller.report_error(f"{self.pool.hosts[board]}: {error}")

    @QtCore.pyqtSlot()
    def updateSpatialSpectrum(self):
        now = time.monotonic()
        maximum_age = float(self.appconfig.get("visualization", "max_age"))
        selected = self.advertisers.selected
        shape = (self.overlay.resolutionAzimuth, self.overlay.resolutionElevation)
        self._spectra = {key: value for key, value in self._spectra.items() if key[0] in selected and now - value[0] < maximum_age and value[1].shape == shape}
        image = np.zeros(shape)
        directions, labels = [], []
        if not self.controller.busy:
            by_source = {}
            for (source, board), record in self._spectra.items():
                by_source.setdefault(source, []).append(record)
            for source, records in by_source.items():
                intensity, paths = combine_board_spectra(records, now, maximum_age, bartlett=self.appconfig.get("beamformer", "type") == "Bartlett")
                image += intensity
                directions.extend(paths)
                labels.extend([source.split(":", 1)[-1]] * len(paths))
        polarized_sources = []
        coherency = np.zeros((*image.shape, 2, 2), complex) if self.overlay.polarizationVisible else None
        for key, (received, power, _) in self._spectra.items():
            if self.controller.busy:
                break
            polarization = self._polarizations.get(key)
            if coherency is not None and polarization is not None and now - polarization[0] < float(self.appconfig.get("beamformer", "backlog_age")):
                jones = polarization[1]
                polarized_sources.append(jones)
                weight = power * measurement_opacity(now - received, maximum_age)
                coherency += weight[..., None, None] * jones[..., :, None] * jones.conj()[..., None, :]
        self._polarizations = {key: value for key, value in self._polarizations.items() if key in self._spectra}
        self.overlay.publish_markers(directions, labels=labels)
        if coherency is not None:
            if len(polarized_sources) == 1 and self.appconfig.get("beamformer", "type") == "Bartlett":
                dominant = polarized_sources[0]
                if self._polarization_field is not None:
                    overlap = np.sum(self._polarization_field.conj() * dominant)
                    dominant = dominant * np.exp(-1j * np.angle(overlap))
            else:
                dominant = polarization_ellipse(coherency, self._polarization_field)
            self._polarization_field = dominant
            # Match the WiFi overlay: weak beams have smaller ellipses.
            power = np.trace(coherency, axis1=-2, axis2=-1).real
            if not self.appconfig.get("beamformer", "normalize_polarization"):
                dominant = dominant * np.sqrt(power / max(float(power.max()), 1e-30))[..., None]
            self.overlay.publish_polarization(dominant[..., 1], dominant[..., 0])
        self.overlay.publish_spatial_intensity(image)

    @QtCore.pyqtProperty(bool, notify=stateChanged)
    def busy(self):
        return self.controller.busy

    def exec(self):
        self.camera_view.start()
        return super().exec()

    def onAboutToQuit(self):
        if getattr(self, "_closing", True):
            return
        try:
            super().onAboutToQuit()
        finally:
            self.camera_view.stop()


def main():
    try:
        return BLECamera(sys.argv).exec()
    except CameraUnavailableError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
