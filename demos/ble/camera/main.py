#!/usr/bin/env python
"""BLE advertiser directions over a live camera image."""

from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys
import time
import numpy as np
from PyQt6 import QtCore

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from demos.common.latest_worker import LatestWorker
from demos.common.application import CombinedArrayMixin, ESPARGOSApplication
from demos.common.camera.view import CameraView
from demos.common.camera.video import CameraUnavailableError
from demos.common.camera.overlay import CameraOverlayModel
from demos.ble.common.advertisers import Advertisers, advertiser_record
from demos.ble.common.controller import BLEController
from espargos import BufferPolicy
from espargos.ble_beamforming import BLEBeamformer
from demos.ble.common.beamforming import processing_config, render_spectrum, render_polarization, measurement_opacity, spectrum_intensity
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


class BLECamera(CombinedArrayMixin, ESPARGOSApplication):
    packetArrived = QtCore.pyqtSignal(dict)
    spectrumArrived = QtCore.pyqtSignal(str, object, float)
    stateChanged = QtCore.pyqtSignal()
    errorOccurred = QtCore.pyqtSignal(str)
    DEFAULT_CONFIG = {
        "camera": CameraView.DEFAULT_CONFIG,
        "receiver": dict(BLEController.DEFAULT_CONFIG, advertisers=[]),
        "beamformer": {
            "type": "Bartlett",
            "backlog_size": 16,
            "backlog_age": 0.25,
            "resolution_azimuth": 128,
            "resolution_elevation": 96,
            "polarization_mode": "ignore",
            "multipath": True,
            "polarization_speed": 2.0,
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
        },
    }

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        parser.add_argument("--no-camera", action="store_true", help="Show the radio overlay without a video camera")

    def __init__(self, argv):
        CameraView.configure_rendering()
        parser = argparse.ArgumentParser(description="ESPARGOS BLE camera", add_help=False)
        super().__init__(argv, argparse_parent=parser)
        if self.args.no_camera:
            self.appconfig.set({"camera": {"enable": False}})
        self._closing = False
        self._spectra = {}
        self._directions = {}
        self._polarizations = {}
        self._polarization_field = None
        self._beamformer = BLEBeamformer()
        self._last_list_update = 0.0
        self._measurement_after = time.monotonic()
        self.receiver = None
        self._phase_error_reported = False
        signal.signal(signal.SIGINT, lambda *_: QtCore.QMetaObject.invokeMethod(self, "quit", QtCore.Qt.ConnectionType.QueuedConnection))
        self._interrupt_timer = QtCore.QTimer(self)
        self._interrupt_timer.timeout.connect(lambda: None)
        self._interrupt_timer.start(200)
        config = self.appconfig.get("receiver")
        if self.appconfig.get("beamformer", "polarization_mode") != "ignore":
            config = dict(config, polarization=4)
            self.appconfig.set({"receiver": config})
        self.controller = BLEController({k: v for k, v in config.items() if k != "advertisers"}, buffering=BufferPolicy(capacity=256, max_age=0.5, batch_size=64), logger=self.logger, parent=self)
        self.controller.operationStarted.connect(self._operation_started)
        self.controller.stateChanged.connect(self.stateChanged)
        self.controller.configChanged.connect(self._receiver_config_changed)
        self.controller.connected.connect(self._connected)
        self.advertisers = Advertisers(self, (mac.lower() for mac in self.appconfig.get("receiver", "advertisers")))
        self._selected = frozenset()
        self.advertisers.selectionChanged.connect(self._selection_changed)
        self._beamformer_epoch = self._measurement_after
        self._phase_worker = LatestWorker(self._process_measurement)
        self.packetArrived.connect(self._observe_advertiser)
        self.spectrumArrived.connect(self._spectrum)
        self.errorOccurred.connect(self._receiver_error)
        self.camera_view = CameraView(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        self.video_camera = self.camera_view.initialize()
        self.overlay = BLEOverlay(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        self.overlay.attach_to_engine(self.engine)
        self._processing_signature = None
        self.appConfigChanged.connect(self._processing_config_changed)
        self.appconfig.updateUIState.connect(self._processing_config_changed)
        self.initialize_qml(
            Path(__file__).with_name("Main.qml"),
            {
                "CameraView": self.camera_view,
                "WebCam": self.video_camera,
                "overlayModel": self.overlay,
                "advertisers": self.advertisers,
                "bleController": self.controller,
            },
        )

    def _open_pool(self):
        self._prepare_pool_init({})
        if len(self.pool.hosts) != 1:
            raise ValueError("BLE camera currently supports one eight-sensor array")
        return self.pool

    def _operation_started(self):
        self._measurement_after = time.monotonic()
        self._phase_worker.clear()
        self._spectra.clear()
        self._polarizations.clear()
        self._polarization_field = None
        self._phase_error_reported = False

    def _receiver_config_changed(self, config):
        self.appconfig.set({"receiver": config})
        self._processing_config_changed()

    def _processing_config_changed(self):
        config = self.appconfig.get("receiver")
        signature = (processing_config(self.appconfig.get("beamformer")), config["channel"], self.overlay.resolutionAzimuth, self.overlay.resolutionElevation)
        if signature == self._processing_signature:
            return
        self._processing_signature = signature
        self._operation_started()
        self._steering = steering_vectors_2d(2, 4, signature[2], signature[3], frequency_hz=channel_frequency(config["channel"]), pixel_centers=True)

    def _connected(self):
        self.receiver = self.controller.receiver
        self._receiver_config_changed(self.controller.receiverConfig)
        self.receiver.add_callback(lambda cluster: self._packet(cluster.packet), lambda cluster: True)
        self.receiver.add_callback(self._measured)
        self.ready = True
        self.initComplete.emit()

    def _packet(self, packet):
        if packet.rf_switch == 1 or not packet.is_advertisement:
            return
        self.packetArrived.emit(advertiser_record(packet))

    @QtCore.pyqtSlot(dict)
    def _observe_advertiser(self, record):
        self.advertisers.observe(record)
        self._selection_changed()

    def _selection_changed(self):
        self._selected = frozenset(self.advertisers.selected)

    def _measured(self, cluster):
        packet = cluster.packet
        identity = packet.address_type + ":" + (packet.advertiser or "")
        if self.controller.busy or cluster.is_calibration or identity not in self._selected or not self.controller.calibrationSupported:
            return
        self._phase_worker.submit(identity, (cluster, time.monotonic()))

    def _process_measurement(self, item):
        cluster, received = item
        if self._beamformer_epoch != self._measurement_after:
            self._beamformer.clear()
            self._beamformer_epoch = self._measurement_after
        packet = cluster.packet
        identity = packet.address_type + ":" + (packet.advertiser or "")
        if self.controller.busy or identity not in self._selected:
            return
        try:
            iq = self.receiver.packet_iq(cluster)
            phase = iq.phase()
        except BLECalibrationError as error:
            if self.controller.busy or received < self._measurement_after:
                return
            if not self._phase_error_reported:
                self.errorOccurred.emit(str(error))
                self._phase_error_reported = True
            return
        self._phase_error_reported = False
        if np.count_nonzero(phase.valid[0]) >= 2:
            # An operation may have completed while this observation was fitted.
            if received < self._measurement_after:
                return
            steering = self._steering
            covariance, sample_counts = iq.covariance()
            result = self._beamformer.process(
                identity,
                phase.vector[0],
                cluster.rf_switch_state[0],
                received,
                steering,
                channel_frequency(packet.channel),
                processing_config(self.appconfig.get("beamformer")),
                amplitude=phase.amplitude[0] if phase.amplitude is not None else None,
                orientations=self.antenna_orientations,
                covariance=covariance, sample_counts=sample_counts,
            )
            power = render_spectrum(result, steering.shape[2:])
            directions, jones = result.directions.tolist(), render_polarization(result, steering.shape[2:])
            # A sparse random-feed window may temporarily lack coverage. Keep
            # the last usable frame until its normal age limit expires.
            if np.any(power > 0):
                self.spectrumArrived.emit(identity, (power * float(np.min(phase.coherence[0][phase.valid[0]])), directions, jones), received)

    @QtCore.pyqtSlot(str, object, float)
    def _spectrum(self, identity, result, received):
        if self.controller.busy or identity not in self.advertisers.selected or received < self._measurement_after:
            return
        if time.monotonic() - received >= float(self.appconfig.get("visualization", "max_age")):
            return
        power, directions, jones = result
        if power.shape != (self.overlay.resolutionAzimuth, self.overlay.resolutionElevation):
            return
        self._spectra[identity] = (received, power)
        if not directions and np.any(power):
            a, e = np.unravel_index(np.argmax(power), power.shape)
            directions = [[(2 * a + 1) / power.shape[0] - 1, (2 * e + 1) / power.shape[1] - 1]]
        self._directions[identity] = directions
        if jones is not None:
            self._polarizations[identity] = (received, jones)

    @QtCore.pyqtSlot(str)
    def _receiver_error(self, error):
        self._spectra.clear()
        self.controller.report_error(error)

    @QtCore.pyqtSlot()
    def updateSpatialSpectrum(self):
        now = time.monotonic()
        if now - self._last_list_update >= 0.5:
            self.advertisers.refresh()
            self._last_list_update = now
        maximum_age = float(self.appconfig.get("visualization", "max_age"))
        selected = self.advertisers.selected
        shape = (self.overlay.resolutionAzimuth, self.overlay.resolutionElevation)
        self._spectra = {key: value for key, value in self._spectra.items() if key in selected and now - value[0] < maximum_age and value[1].shape == shape}
        image = np.zeros(shape)
        if not self.controller.busy:
            for received, spectrum in self._spectra.values():
                # Apply age to display opacity after cubic contrast, not to
                # measurement power. No brightness ramp between fresh packets.
                opacity = measurement_opacity(now-received, maximum_age)
                image += spectrum_intensity(spectrum) * opacity
        self._directions = {key: value for key, value in self._directions.items() if key in self._spectra}
        directions, labels = [], []
        coherency = np.zeros((*image.shape, 2, 2), complex)
        for key, paths in self._directions.items():
            if self.controller.busy:
                break
            directions.extend(paths)
            labels.extend([key.split(":", 1)[1]] * len(paths))
            polarization = self._polarizations.get(key)
            if polarization is not None and now-polarization[0] < float(self.appconfig.get("beamformer", "backlog_age")):
                jones = polarization[1]
                received, power = self._spectra[key]
                weight = power * measurement_opacity(now-received, maximum_age)
                coherency += weight[..., None, None] * jones[..., :, None] * jones.conj()[..., None, :]
        self._polarizations = {key: value for key, value in self._polarizations.items() if key in self._spectra}
        self.overlay.publish_markers(directions, labels=labels)
        if self.overlay.polarizationVisible:
            dominant = polarization_ellipse(coherency, self._polarization_field)
            self._polarization_field = dominant
            # Match the WiFi overlay: weak beams have smaller ellipses.
            power = np.trace(coherency, axis1=-2, axis2=-1).real
            dominant = dominant * np.sqrt(power / max(float(power.max()), 1e-30))[...,None]
            self.overlay.publish_polarization(dominant[..., 1], dominant[..., 0])
        self.overlay.publish_spatial_intensity(image)

    @QtCore.pyqtProperty(bool, notify=stateChanged)
    def busy(self):
        return self.controller.busy

    def exec(self):
        self.camera_view.start()
        self.controller.start(self._open_pool)
        try:
            return super().exec()
        finally:
            self.onAboutToQuit()

    def onAboutToQuit(self):
        if getattr(self, "_closing", True):
            return
        self._closing = True
        try:
            self.controller.close()
        finally:
            self._phase_worker.close()
            self.camera_view.stop()
            super().onAboutToQuit()


def main():
    try:
        return BLECamera(sys.argv).exec()
    except CameraUnavailableError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
