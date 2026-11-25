#!/usr/bin/env python
"""Live direction-finding overlay for arbitrary signals, computed from raw IQ.

Every capture event (an Interval chunk, an Accumulate vector, or a Signal
capture) is searched for emissions above the receiver's noise floor. Each
emission is located on its own and entered into a decaying spatial image, so
emitters that share a band but transmit at different times stay separate.
"""

import argparse
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace
import pathlib
import sys
import threading
import queue
import time

sys.path.append(str(pathlib.Path(__file__).absolute().parents[3]))

# The DSP worker solves many small covariance matrices. Extra BLAS threads
# compete with packet reception and video decoding on the same machine.
if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np
import PyQt6.QtCore

import espargos
from demos.common.camera.view import CameraView
from demos.common.application import CombinedArrayMixin
from espargos import CHUNK_SAMPLES
from demos.iq.common.application import ESPARGOSIQApplication
from demos.iq.camera.processing import DirectionHistory, peak_directions
from demos.iq.camera.worker import process_capture, initialize_processor
from demos.iq.common.iq_waterfall import WaterfallImageProvider

try:
    from .iq_overlay import IQCameraOverlay
except ImportError:
    from iq_overlay import IQCameraOverlay


ADC_FULL_SCALE = 512.0
WATERFALL_HEIGHT = 180
STATE_POLL_INTERVAL_S = 2.0
# Signal-mode trigger threshold in standard deviations of the noise in I and Q.
# The sensors compare single samples, and captures are a scarce resource (about
# ten per second): a lower threshold spends them on noise and marginal signals.
TRIGGER_THRESHOLD_SIGMA = 12.0


class EspargosIQCamera(CombinedArrayMixin, ESPARGOSIQApplication):
    """IQ camera for a rectangular ESPARGOS combined array."""

    autoThresholdChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "camera": CameraView.DEFAULT_CONFIG,
        "capture": {
            "rf_freq_hz": 2472000000,
            "capture_path": "wifi",
            "adc_decimation": 1,
            "adc_source_sel": 15,
            "filter_bw_mhz": 20,
            "gain_mode": "manual",
            "rx_gain": 35,
            "rf_switch": 2,
            "trigger": {"mode": "auto", "threshold": 64, "antennas": list(range(8)), "holdoff_ms": 10, "capture_chunks": 56, "quiet_threshold": 0},
        },
        "receiver": {
            "auto_threshold": True,
        },
        "beamformer": {
            "type": "MUSIC",
            "source_dynamic_range_db": 12.0,
            "threshold_db": 6.0,
            "min_purity": 0.3,
            "min_bandwidth_mhz": 0.0,
            "reject_dc": True,
            "band_limit": False,
            "band_low_mhz": 2400.0,
            "band_high_mhz": 2500.0,
            "persistence_s": 0.20,
            "resolution_azimuth": 128,
            "resolution_elevation": 64,
        },
        "visualization": {
            "space": "camera",
            "waterfall": False,
            "markers": True,
            "edge_markers": True,
            "dynamic_range_db": 12.0,
            "display_mode": "flashes",
            "flash_width_deg": 4.0,
            "azimuth_correction": 0.0,
            "elevation_correction": 0.0,
        },
    }

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        parser.add_argument("--no-camera", action="store_true", help="Show the radio overlay without a video camera")

    def __init__(self, argv):
        CameraView.configure_rendering()
        parser = argparse.ArgumentParser(description="ESPARGOS Demo: IQ camera overlay for arbitrary signals", add_help=False)
        super().__init__(argv, argparse_parent=parser)
        if self.args.no_camera:
            self.appconfig.set({"camera": {"enable": False}})

        self.iqcontrol = None
        self._window = (np.hanning(CHUNK_SAMPLES) / (np.sum(np.hanning(CHUNK_SAMPLES)) * ADC_FULL_SCALE)).astype(np.float32)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pending = queue.Queue(maxsize=1)
        self._dsp_executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        self._dsp_executor.submit(initialize_processor)
        self.dsp_ms = 0.0
        self._processor = threading.Thread(target=self._process_pending, daemon=True)
        self.processed_captures = 0
        self.dropped_captures = 0
        self.queue_dropped_captures = 0
        self.late_captures = 0
        self.capture_to_process_ms = 0.0
        self._manager = threading.Thread(target=self._manage_capture, daemon=True)
        self._status = "Synchronizing"  # overrides the calibration status while set
        self._noise = None
        self._correction = None
        self._correction_source = None
        self._capture_time = 0.0
        self._latest_signal_identity = None
        self._reset_image()
        self.appConfigChanged.connect(self._view_changed)

        self.waterfall = WaterfallImageProvider(CHUNK_SAMPLES, WATERFALL_HEIGHT)
        self.engine.addImageProvider("iq-camera-waterfall", self.waterfall)
        self.camera_view = CameraView(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        self.video_camera = self.camera_view.initialize()

    def _reset_image(self):
        self._cleared_at = time.monotonic()
        self._history = DirectionHistory()
        self._render_key = None
        self._emission_time = 0.0
        self._waterfall_dirty = False
        self._waterfall_row_us = 0.0
        self._span_mhz = (0.0, 0.0)
        self._strongest = None  # (weight, power, low MHz, high MHz, purity) within the current GUI tick

    @PyQt6.QtCore.pyqtProperty("QVariantMap", constant=True)
    def triggerDefaults(self):
        return {
            "interval": {"interval_chunks": 4096, "burst_chunks": 1, "offset_chunks": 0},
            "accumulate": {"interval_chunks": 4096, "vector_chunks": 4, "offset_chunks": 0},
            "signal": self.DEFAULT_CONFIG["capture"]["trigger"],
        }

    def _create_receiver(self, owner):
        capture = dict(self.appconfig.get("capture"))
        trigger = capture["trigger"]
        # ConfigManager merges dictionaries, so changing just the mode on the
        # command line otherwise leaves the default Signal-only fields behind.
        defaults = self.triggerDefaults
        mode = trigger["mode"]
        if mode == "auto":
            mode = "signal" if len(owner.boards) == 1 else "interval"
        if mode not in defaults:
            raise ValueError(f"Unknown capture trigger mode: {mode}")
        capture["trigger"] = {"mode": mode, **{key: trigger.get(key, value) for key, value in defaults[mode].items() if key != "mode"}}
        if len(owner.boards) > 1 and capture["trigger"]["mode"] == "signal":
            raise ValueError("Signal trigger requires one board; choose capture.trigger.mode=interval or accumulate for a combined array")
        receiver = super()._create_receiver(owner)
        try:
            receiver.apply_config(capture)
        except Exception as error:
            # initialize_pool closes the connection when receiver setup fails.
            # Release the sensors while the controller is still reachable.
            try:
                owner.configure(mode="wifi")
            except Exception as cleanup:
                error.add_note(f"Could not restore WiFi mode: {cleanup}")
            raise
        self._capture_config = capture
        return receiver

    # ---- capture events (pool processing thread) ----

    def _on_iq_cluster(self, cluster):
        if np.all(cluster.capture_id == 0xFFFFFFFF):  # chunks of Signal captures arrive assembled in _on_capture
            self._on_capture(cluster)

    def _on_capture(self, source):
        received_at = time.monotonic()
        self._capture_time = received_at
        if self._stop.is_set() or self.receiver.calibrating or not self.receiver.calibration_applies_to(source):
            return
        if isinstance(source, espargos.IQSignalCapture):
            identity = (int(np.ravel(source.config_generation)[0]), int(source.capture_id))
            previous = self._latest_signal_identity
            if previous is not None and previous[0] == identity[0] and not 0 < (identity[1] - previous[1]) & 0xFFFFFFFF < 0x80000000:
                # Replay can finish an older event after a newer one. Keep
                # its validation/ACK in the receiver, but never rewind video.
                self.dropped_captures += 1
                self.late_captures += 1
                return
            self._latest_signal_identity = identity
        try:
            self._pending.put_nowait((received_at, source))
        except queue.Full:
            try:
                self._pending.get_nowait()
            except queue.Empty:
                pass
            self.dropped_captures += 1
            self.queue_dropped_captures += 1
            self._pending.put_nowait((received_at, source))

    def _process_pending(self):
        while not self._stop.is_set():
            try:
                received_at, source = self._pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if received_at < self._cleared_at:
                    continue
                if self._locate_emissions(source, received_at):
                    self.processed_captures += 1
            except Exception:
                self.logger.exception("Could not process a capture")
            self.capture_to_process_ms = (time.monotonic() - received_at) * 1000

    def _locate_emissions(self, source, received_at):
        """Locate the emissions in one capture event and add them to the image."""

        generations = np.asarray(source.config_generation).reshape(len(source.config_generation), -1)
        if self.receiver.calibrating or not np.all(generations == generations[:, :1]):
            return
        if not self.receiver.calibration_applies_to(source):
            return
        now = time.monotonic()
        beamformer = self.appconfig.get("beamformer")
        sample_rate = float(np.ravel(source.sample_rate_hz)[0])
        center = float(np.ravel(source.center_freq_hz)[0])

        calibration = self.receiver.calibration
        if calibration is None:
            return
        if calibration is not self._correction_source:
            correction = self.receiver.cal_correction(CHUNK_SAMPLES)
            if correction is None:
                return
            self._correction = self._combine(correction).astype(np.complex64).reshape(-1, 1, CHUNK_SAMPLES)
            self._correction_source = calibration
        samples = self._combine(source.iq).reshape(self.n_rows * self.n_cols, -1, CHUNK_SAMPLES)
        # AGC can choose a new gain on every chunk. Reset the noise estimator
        # for commanded hardware changes, never for one sensor's first sample.
        noise_key = (type(source).__name__, sample_rate, center, tuple(generations[:, 0]))
        args = (samples, self._correction, self._window, (self.n_rows, self.n_cols), center, sample_rate, noise_key, dict(beamformer), dict(self.appconfig.get("visualization")), now)
        result = self._dsp_executor.submit(process_capture, *args).result()
        self.dsp_ms = result["dsp_ms"]
        with self._lock:
            if received_at < self._cleared_at or calibration is not self.receiver.calibration:
                return
            self._noise = SimpleNamespace(power=result["noise"])
            if result["rows"] is not None:
                self.waterfall.add_rows(result["rows"])
                self._waterfall_dirty = True
            self._waterfall_row_us = CHUNK_SAMPLES / sample_rate * 1e6
            self._span_mhz = result["span"]
            if result["image"] is not None:
                self._history.add(result["image"], received_at, max(beamformer["persistence_s"], 1e-3))
            strongest = result["strongest"]
            if strongest is not None and (self._strongest is None or strongest[0] > self._strongest[0]):
                self._strongest = strongest
        return True

    def _combine(self, data):
        """Map board-local sensor data into the configured rectangular array."""
        return espargos.combined_array.build_combined_array_data(self.indexing_matrix, np.asarray(data)[np.newaxis, ...])[0]

    # ---- display (GUI thread) ----

    @PyQt6.QtCore.pyqtSlot()
    def updateSpatialSpectrum(self):
        if self._stop.is_set():
            return
        now = time.monotonic()
        with self._lock:
            image = self._history.power
            opacity = self._history.opacity(now, max(self.appconfig.get("beamformer", "persistence_s"), 1e-3))
            render_key = (self._history.received, round(255 * opacity))
            waterfall_dirty, strongest = self._waterfall_dirty, self._strongest
            span, row_us = self._span_mhz, self._waterfall_row_us
            self._waterfall_dirty = False
            self._strongest = None

        if self._status is not None:
            self.overlay.set_capture_status(self._status)
        elif self.iqcontrol.referenceBusy or self.receiver.calibrating:
            self.overlay.set_capture_status("Calibrating")
        elif self.iqcontrol.calibrationError:
            self.overlay.set_capture_status("Calibration failed · open RX for details")
        elif self.iqcontrol.syncNeeded or self.receiver.calibration is None:
            self.overlay.set_capture_status("Calibration needed · open RX to calibrate")
        else:
            self.overlay.set_capture_status("Calibrated")

        if image is not None and render_key != self._render_key:
            self._render_key = render_key
            peak = float(image.max())
            self.overlay.publish_spatial_spectrum(image, peak, opacity)
            visible = peak * 10.0 ** (-self.appconfig.get("visualization", "dynamic_range_db") / 10.0)
            markers = opacity > 0.1 and self.appconfig.get("visualization", "markers") and self.appconfig.get("visualization", "display_mode") != "azimuth"
            self.overlay.publish_markers(peak_directions(image, visible) if markers else [])
        if waterfall_dirty:
            self.overlay.publish_waterfall(*span, row_us)
        if strongest is not None:
            _weight, power, low, high, purity = strongest
            self.overlay.publish_emission(10.0 * np.log10(max(power, 1e-12)), low, high, purity)
            self._emission_time = now
        elif self._emission_time and now - self._emission_time > 2.0:
            self._emission_time = 0.0
            self.overlay.publish_emission(float("-inf"), None, None, None)

    @PyQt6.QtCore.pyqtSlot()
    def clearImage(self):
        with self._lock:
            self._reset_image()
            self.waterfall.clear()
        self.overlay.publish_waterfall(0.0, 0.0, 0.0)
        self.overlay.publish_emission(-np.inf, None, None, None)
        self.overlay.publish_spatial_spectrum(np.zeros((self.overlay.resolutionAzimuth, self.overlay.resolutionElevation)), 1)
        self.overlay.publish_markers([])

    def _view_changed(self, config):
        self._render_key = None
        if "beamformer" in config or "display_mode" in config.get("visualization", {}):
            self.clearImage()

    # ---- capture management (worker thread) ----

    def _capture_config_applied(self, patch):
        changed_feed = "rf_switch" in patch and patch["rf_switch"] != self._capture_config.get("rf_switch")
        self._capture_config = {**self._capture_config, **patch}
        if changed_feed or self.iqcontrol.syncNeeded:
            self.clearImage()

    def _reconnect_capture(self):
        """Restore the camera settings if a reboot returned sensors to WiFi."""
        with self.iqcontrol._reference_io_lock:
            self.pool.reconnect()
            self.receiver.clear_chunks()
            self._latest_signal_identity = None
            if any(any(mode != "iq_rx" for mode in board.iq.get_config()["modes"]) for board in self.pool.boards):
                self.pool.configure(mode="iq_rx")
                self.receiver.apply_config(self._capture_config)
                self.iqcontrol._set_sync_needed(True)

    def _manage_capture(self):
        """Monitor receiver state without blocking capture or the GUI."""
        reconnect = False
        while not self._stop.wait(STATE_POLL_INTERVAL_S):
            try:
                if self.iqcontrol.referenceBusy or self.receiver.calibrating:
                    continue
                reconnect = reconnect or not all(board.is_stream_connected for board in self.pool.boards)
                if reconnect:
                    self._status = "Reconnecting receiver"
                    self._reconnect_capture()
                    reconnect = False
                config = self.receiver.get_config()
                self.iqcontrol._check_calibration_config(config)
                if self.appconfig.get("receiver", "auto_threshold"):
                    self._adjust_trigger_threshold(config)
                self._status = None
            except Exception as error:
                self._status = str(error)
                self.logger.warning("Capture management: %s", error)

    @PyQt6.QtCore.pyqtProperty(bool, notify=autoThresholdChanged)
    def autoThreshold(self):
        return self.appconfig.get("receiver", "auto_threshold")

    @PyQt6.QtCore.pyqtSlot(bool)
    def setAutoThreshold(self, enabled):
        if enabled != self.autoThreshold:
            self.appconfig.set({"receiver": {"auto_threshold": enabled}})
            self.autoThresholdChanged.emit()

    def _adjust_trigger_threshold(self, config):
        trigger = dict(config.get("trigger", {}))
        if trigger.get("mode") != "signal":
            return
        previous = trigger["threshold"]
        noise = self._noise.power if self._noise is not None else None
        if time.monotonic() > self._capture_time + 3.0:
            threshold = max(8, previous // 2)
        elif noise is not None:
            threshold = int(TRIGGER_THRESHOLD_SIGMA * ADC_FULL_SCALE * np.sqrt(np.sum(noise) / 3.0))
        else:
            return
        threshold = int(np.clip(threshold, max(8, trigger.get("quiet_threshold", 0) + 1), 511))
        if abs(threshold - previous) > 0.2 * previous:
            with self.iqcontrol._reference_io_lock:
                if self.iqcontrol.referenceBusy or not self.autoThreshold:
                    return
                # A GUI edit can have changed mode or trigger settings since
                # the poll. Never restore that stale snapshot over the edit.
                current = self.receiver.get_config().get("trigger", {})
                if current != trigger:
                    return
                self.receiver.apply_config({"trigger": {**current, "threshold": threshold}})

    def _on_calibration_changed(self):
        self._status = None
        self.clearImage()

    def _start_iq_camera(self):
        self.iqcontrol = self.create_iq_controller()
        self.iqcontrol.calibrationChanged.connect(self._on_calibration_changed)
        self.iqcontrol.configApplied.connect(self._capture_config_applied)
        self.engine.rootContext().setContextProperty("iqcontrol", self.iqcontrol)
        self.receiver.add_callback(self._on_iq_cluster)
        self.receiver.add_accumulation_callback(self._on_capture)
        if len(self.pool.boards) == 1:
            self.receiver.add_signal_capture_callback(self._on_capture)
        self._processor.start()
        self.iqcontrol.sync_async()
        self._manager.start()

    def onAboutToQuit(self):
        if getattr(self, "_shutdown_started", False):
            return
        self._shutdown_started = True
        # Capture management must not reconfigure the array while it returns to WiFi mode
        self._stop.set()
        if self._manager.is_alive():
            self._manager.join()
        if self._processor.is_alive():
            self._processor.join()
        self._dsp_executor.shutdown(wait=True, cancel_futures=True)
        self.camera_view.stop()
        if self.iqcontrol is not None:
            self.iqcontrol.close()
            with self.iqcontrol._reference_io_lock:
                super().onAboutToQuit()
        else:
            super().onAboutToQuit()

    def exec(self):
        try:
            return self._exec_camera()
        finally:
            self.onAboutToQuit()

    def _exec_camera(self):
        self.initComplete.connect(self._start_iq_camera)
        self.initialize_pool(calibrate=False)
        self.overlay = IQCameraOverlay(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        self.overlay.attach_to_engine(self.engine)
        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "Main.qml",
            context_props={
                "iqcontrol": None,
                "CameraView": self.camera_view,
                "overlayModel": self.overlay,
                "WebCam": self.video_camera,
            },
        )
        self.camera_view.start()
        return super().exec()


if __name__ == "__main__":
    application = EspargosIQCamera(sys.argv)
    sys.exit(application.exec())
