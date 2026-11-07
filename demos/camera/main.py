#!/usr/bin/env python
"""Live direction-finding overlay for arbitrary signals, computed from raw IQ.

Every capture event (an Interval chunk, an Accumulate vector, or a Signal
capture) is searched for emissions above the receiver's noise floor. Each
emission is located on its own and entered into a decaying spatial image, so
emitters that share a band but transmit at different times stay separate.
"""

import argparse
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace
import pathlib
import sys
import threading
import queue
import time

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import numpy as np
import PyQt6.QtCore

import espargos
from demos.common.camera.view import CameraView
from demos.common.application import CombinedArrayMixin
from espargos import CHUNK_SAMPLES
from demos.iq.common.application import ESPARGOSIQApplication
from demos.camera.processing import peak_directions
from demos.camera.worker import process_capture, initialize_processor
from demos.iq.common.iq_waterfall import WaterfallImageProvider

try:
    from .iq_overlay import IQCameraOverlay
except ImportError:
    from iq_overlay import IQCameraOverlay


ADC_FULL_SCALE = 512.0
WATERFALL_HEIGHT = 180
# The image is displayed relative to its recent peak, which decays this slowly:
# an emitter that goes quiet fades out instead of being renormalized, and the
# weak, misplaced response of the cross-polarized feed stays dim in between
# the events of the matching feed.
REFERENCE_DECAY_S = 1.5
FEED_SWITCH_INTERVAL_S = 0.6
STATE_POLL_INTERVAL_S = 2.0
# Signal-mode trigger threshold in standard deviations of the noise in I and Q.
# The sensors compare single samples, and captures are a scarce resource (about
# ten per second): a lower threshold spends them on noise and marginal signals.
TRIGGER_THRESHOLD_SIGMA = 12.0
RF_SWITCH_FEEDS = (2, 3)  # antenna feed R, antenna feed L


class EspargosIQCamera(CombinedArrayMixin, ESPARGOSIQApplication):
    """IQ camera for a rectangular ESPARGOS combined array."""

    requestCalibration = PyQt6.QtCore.pyqtSignal()
    liveStatsChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "camera": CameraView.DEFAULT_CONFIG,
        "capture": {
            "rf_freq_hz": 2472000000,
            "adc_decimation": 1,
            "adc_source_sel": 15,
            "filter_bw_mhz": 20,
            "gain_mode": "manual",
            "rx_gain": 35,
            "rf_switch": 2,
            "trigger": {"mode": "signal", "threshold": 64, "antennas": list(range(8)), "holdoff_ms": 10, "capture_chunks": 56, "quiet_threshold": 0},
        },
        "receiver": {
            "both_feeds": True,
            "auto_calibration": True,
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
            "persistence_s": 0.12,
            "resolution_azimuth": 128,
            "resolution_elevation": 64,
        },
        "visualization": {
            "space": "camera",
            "waterfall": True,
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
        parser.add_argument("--no-camera", action="store_true", help="Show the spatial overlay on black instead of opening a video camera")

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
        self._capture_arrived = threading.Event()
        self._pending = queue.Queue(maxsize=3)
        self._dsp_executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        self._dsp_executor.submit(initialize_processor)
        self.dsp_ms = 0.0
        self._processor = threading.Thread(target=self._process_pending, daemon=True)
        self.processed_captures = 0
        self.dropped_captures = 0
        self.queue_dropped_captures = 0
        self.late_captures = 0
        self.processing_ms = 0.0
        self.capture_to_process_ms = 0.0
        self._last_processed_at = 0.0
        self._manager = threading.Thread(target=self._manage_capture, daemon=True)
        self._status = "Synchronizing"  # overrides the calibration status while set
        self._noise = None
        self._correction = None
        self._correction_source = None
        self._calibrated_state = None
        self._stale_events = 0
        self._capture_time = 0.0
        self._latest_signal_identity = None
        self._reset_image()
        self._frozen = False
        self._stats_time = time.monotonic()
        self._stats_count = 0
        self._capture_rate = 0.0
        self.appConfigChanged.connect(self._view_changed)

        self.waterfall = WaterfallImageProvider(CHUNK_SAMPLES, WATERFALL_HEIGHT)
        self.engine.addImageProvider("iq-camera-waterfall", self.waterfall)
        self.camera_view = CameraView(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        self.video_camera = self.camera_view.initialize()

    def _reset_image(self):
        self._image = None
        self._image_time = 0.0
        self._reference = 0.0
        self._reference_time = 0.0
        self._emission_time = 0.0
        self._waterfall_dirty = False
        self._waterfall_row_us = 0.0
        self._span_mhz = (0.0, 0.0)
        self._strongest = None  # (weight, power, low MHz, high MHz, purity) within the current GUI tick

    def _create_receiver(self, owner):
        receiver = super()._create_receiver(owner)
        capture = dict(self.appconfig.get("capture"))
        trigger = capture["trigger"]
        # ConfigManager merges dictionaries, so changing just the mode on the
        # command line otherwise leaves the default Signal-only fields behind.
        defaults = {
            "interval": {"interval_chunks": 1024, "burst_chunks": 1, "offset_chunks": 0},
            "accumulate": {"interval_chunks": 4096, "vector_chunks": 4, "offset_chunks": 0},
            "signal": self.DEFAULT_CONFIG["capture"]["trigger"],
        }
        mode = trigger["mode"]
        if mode not in defaults:
            raise ValueError(f"Unknown capture trigger mode: {mode}")
        capture["trigger"] = {"mode": mode, **{key: trigger.get(key, value) for key, value in defaults[mode].items() if key != "mode"}}
        if len(owner.boards) > 1 and capture["trigger"]["mode"] == "signal":
            raise ValueError("Signal trigger requires one board; choose capture.trigger.mode=interval or accumulate for a combined array")
        receiver.apply_config(capture)
        return receiver

    # ---- capture events (pool processing thread) ----

    def _on_iq_cluster(self, cluster):
        if np.all(cluster.capture_id == 0xFFFFFFFF):  # chunks of Signal captures arrive assembled in _on_capture
            self._on_capture(cluster)

    def _on_capture(self, source):
        self._capture_time = time.monotonic()
        self._capture_arrived.set()
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
            self._pending.put_nowait((time.monotonic(), source))
        except queue.Full:
            try:
                self._pending.get_nowait()
            except queue.Empty:
                pass
            self.dropped_captures += 1
            self.queue_dropped_captures += 1
            self._pending.put_nowait((time.monotonic(), source))

    def _process_pending(self):
        while not self._stop.is_set():
            try:
                received_at, source = self._pending.get(timeout=0.1)
            except queue.Empty:
                continue
            started = time.monotonic()
            try:
                self._locate_emissions(source)
                self.processed_captures += 1
            except Exception:
                self.logger.exception("Could not process a capture")
            self._last_processed_at = time.monotonic()
            self.processing_ms = (self._last_processed_at - started) * 1000
            self.capture_to_process_ms = (self._last_processed_at - received_at) * 1000

    def _locate_emissions(self, source):
        """Locate the emissions in one capture event and add them to the image."""

        if self.receiver.calibrating or any(np.ptp(board) != 0 for board in source.config_generation):
            return
        if not self.receiver.calibration_applies_to(source):
            self._stale_events += 1
            return
        self._stale_events = 0
        now = time.monotonic()
        beamformer = self.appconfig.get("beamformer")
        sample_rate = float(np.ravel(source.sample_rate_hz)[0])
        center = float(np.ravel(source.center_freq_hz)[0])

        calibration = self.receiver.calibration
        if calibration is not self._correction_source:
            self._correction = self._combine(self.receiver.cal_correction(CHUNK_SAMPLES)).astype(np.complex64).reshape(-1, 1, CHUNK_SAMPLES)
            self._correction_source = calibration
        samples = self._combine(source.iq).reshape(self.n_rows * self.n_cols, -1, CHUNK_SAMPLES)
        noise_key = (type(source).__name__, sample_rate, center, int(np.ravel(getattr(source, "sample_rx_gain", 0))[0]))
        args = (samples, self._correction, self._window, (self.n_rows, self.n_cols), center, sample_rate, noise_key, dict(beamformer), dict(self.appconfig.get("visualization")), now)
        result = self._dsp_executor.submit(process_capture, *args).result()
        self.dsp_ms = result["dsp_ms"]
        self._noise = SimpleNamespace(power=result["noise"])
        self.waterfall.add_rows(result["rows"])
        with self._lock:
            self._waterfall_dirty = True
            self._waterfall_row_us = CHUNK_SAMPLES / sample_rate * 1e6
            self._span_mhz = result["span"]
            if self._image is None or self._image.shape != result["image"].shape:
                self._image = np.zeros_like(result["image"])
            self._image *= np.exp(-(now - self._image_time) / max(beamformer["persistence_s"], 1e-3))
            self._image_time = now
            np.maximum(self._image, result["image"], out=self._image)
            strongest = result["strongest"]
            if strongest is not None and (self._strongest is None or strongest[0] > self._strongest[0]):
                self._strongest = strongest

    def _combine(self, data):
        """Map board-local sensor data into the configured rectangular array."""
        return espargos.combined_array.build_combined_array_data(self.indexing_matrix, np.asarray(data)[np.newaxis, ...])[0]

    # ---- display (GUI thread) ----

    @PyQt6.QtCore.pyqtSlot()
    def updateSpatialSpectrum(self):
        if self._stop.is_set():
            return
        now = time.monotonic()
        if now - self._stats_time >= 0.5:
            self._capture_rate = (self.processed_captures - self._stats_count) / (now - self._stats_time)
            self._stats_count, self._stats_time = self.processed_captures, now
            self.liveStatsChanged.emit()
        if self._frozen:
            return
        with self._lock:
            image = None if self._image is None else self._image * np.exp(-(time.monotonic() - self._image_time) / max(self.appconfig.get("beamformer", "persistence_s"), 1e-3))
            waterfall_dirty, strongest, span = self._waterfall_dirty, self._strongest, self._span_mhz
            self._waterfall_dirty = False
            self._strongest = None

        if self._status is not None:
            self.overlay.set_capture_status(self._status)
        elif self.receiver.calibrating:
            self.overlay.set_capture_status("Calibrating")
        elif self.receiver.calibration is None or self._stale_events:
            self.overlay.set_capture_status("Uncalibrated")
        else:
            self.overlay.set_capture_status("Calibrated")

        if image is not None:
            now = time.monotonic()
            display_mode = self.appconfig.get("visualization", "display_mode")
            self._reference = max(float(np.max(image)), self._reference * np.exp(-(now - self._reference_time) / REFERENCE_DECAY_S))
            self._reference_time = now
            self.overlay.publish_spatial_spectrum(image, self._reference)
            visible = self._reference * 10.0 ** (-self.appconfig.get("visualization", "dynamic_range_db") / 10.0)
            self.overlay.publish_markers(peak_directions(image, visible) if self.appconfig.get("visualization", "markers") and display_mode != "azimuth" else [])
        if waterfall_dirty:
            self.overlay.publish_waterfall(*span)
        if strongest is not None:
            _weight, power, low, high, purity = strongest
            self.overlay.publish_emission(10.0 * np.log10(max(power, 1e-12)), low, high, purity)
            self._emission_time = time.monotonic()
        elif time.monotonic() > self._emission_time + 2.0:
            self.overlay.publish_emission(float("-inf"), None, None, None)

    @PyQt6.QtCore.pyqtProperty(str, notify=liveStatsChanged)
    def liveStats(self):
        return f"{self._capture_rate:.0f} captures/s · {self.dsp_ms:.1f} ms DSP · {self.capture_to_process_ms:.0f} ms queued + processing · {self.queue_dropped_captures} processing drops · {self.late_captures} late"

    @PyQt6.QtCore.pyqtProperty(str, notify=liveStatsChanged)
    def waterfallTimeDescription(self):
        return f"{self._waterfall_row_us:.1f} µs/row · capture gaps omitted"

    @PyQt6.QtCore.pyqtSlot(bool)
    def setFrozen(self, frozen):
        self._frozen = bool(frozen)

    @PyQt6.QtCore.pyqtSlot()
    def clearImage(self):
        with self._lock:
            self._reset_image()
        self.overlay.publish_spatial_spectrum(np.zeros((self.overlay.resolutionAzimuth, self.overlay.resolutionElevation)), 1)
        self.overlay.publish_markers([])

    def _view_changed(self, config):
        if "beamformer" in config or "display_mode" in config.get("visualization", {}):
            self.clearImage()

    # ---- capture management (worker thread) ----

    def _manage_capture(self):
        """Calibrate on demand; alternate feeds without blocking Qt or capture ACKs."""
        next_calibration = next_poll = next_feed = 0.0
        feed = 0
        while not self._stop.is_set():
            self._capture_arrived.wait(0.1)
            self._capture_arrived.clear()
            try:
                now = time.monotonic()
                if self.iqcontrol.referenceBusy or self.receiver.calibrating:
                    continue
                if not all(board.is_stream_connected for board in self.pool.boards):
                    self._status = "Reconnecting receiver"
                    with self.iqcontrol._reference_io_lock:
                        self.pool.reconnect()
                        self.receiver.clear_chunks()
                    next_poll = 0.0
                if now >= next_poll:
                    config = self.receiver.get_config()
                    self.iqcontrol._check_calibration_config(config)
                    next_poll = now + 2.0
                    if self.appconfig.get("receiver", "auto_threshold"):
                        self._adjust_trigger_threshold(config)
                if self.iqcontrol.syncNeeded and self.appconfig.get("receiver", "auto_calibration"):
                    if now >= next_calibration:
                        next_calibration = now + 10.0
                        self._status = "Synchronizing"
                        self.requestCalibration.emit()
                    continue
                self._status = None
                if now >= next_feed and self.appconfig.get("receiver", "both_feeds") and (config.get("trigger", {}).get("mode") != "signal" or now - self._capture_time < 0.03 or now - self._capture_time > 2.0):
                    feed ^= 1
                    with self.iqcontrol._reference_io_lock:
                        if not self.iqcontrol.referenceBusy:
                            self.receiver.apply_config({"rf_switch": RF_SWITCH_FEEDS[feed]})
                    next_feed = now + FEED_SWITCH_INTERVAL_S
            except Exception as error:
                self._status = str(error)
                self.logger.warning("Capture management: %s", error)
                self._stop.wait(1.0)

    def _adjust_trigger_threshold(self, config):
        trigger = dict(config.get("trigger", {}))
        if trigger.get("mode") != "signal":
            return
        previous = trigger["threshold"]
        noise = self._noise.power if self._noise is not None else None
        if time.monotonic() > self._capture_time + 3.0:
            threshold = max(8, previous // 2)
        elif noise is not None:
            threshold = int(np.clip(TRIGGER_THRESHOLD_SIGMA * ADC_FULL_SCALE * np.sqrt(np.sum(noise) / 3.0), 8, ADC_FULL_SCALE))
        else:
            return
        if abs(threshold - previous) > 0.2 * previous:
            with self.iqcontrol._reference_io_lock:
                if self.iqcontrol.referenceBusy:
                    return
                # A GUI edit can have changed mode or trigger settings since
                # the poll. Never restore that stale snapshot over the edit.
                current = self.receiver.get_config().get("trigger", {})
                if current != trigger:
                    return
                self.receiver.apply_config({"trigger": {**current, "threshold": threshold}})

    def _on_calibration_changed(self):
        self._stale_events = 0
        self._status = "Calibration failed; retrying" if self.iqcontrol.syncNeeded else None
        with self._lock:
            self._reset_image()

    def _start_iq_camera(self):
        self.iqcontrol = self.create_iq_controller()
        self.iqcontrol.calibrationChanged.connect(self._on_calibration_changed)
        self.engine.rootContext().setContextProperty("iqcontrol", self.iqcontrol)
        self.receiver.add_callback(self._on_iq_cluster)
        self.receiver.add_accumulation_callback(self._on_capture)
        if len(self.pool.boards) == 1:
            self.receiver.add_signal_capture_callback(self._on_capture)
        self.requestCalibration.connect(self.iqcontrol.sync_async)
        self._processor.start()
        self._manager.start()

    def onAboutToQuit(self):
        if getattr(self, "_shutdown_started", False):
            return
        self._shutdown_started = True
        # Capture management must not reconfigure the array while it returns to WiFi mode
        self._stop.set()
        self._capture_arrived.set()
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
        except BaseException:
            self.onAboutToQuit()
            raise

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
