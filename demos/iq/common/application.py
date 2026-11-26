#!/usr/bin/env python

"""Shared Qt controls for IQ demos.

IQController runs Pool configuration and calibration calls without blocking
QML. Receiver-specific controls, such as capture triggers and reference tones,
use Pool.iq. ESPARGOSIQApplication connects these controls to the common shell.
"""

import json
import logging
import signal
import threading

import numpy as np
import PyQt6.QtCore

from demos.common.application import ESPARGOSApplication

from espargos import Pool, iq_tone
from espargos.iq_cluster import CHUNK_SAMPLES
from espargos.iq_rx_stream import receive_sample_rate
from espargos.iq_receiver import IQReceiver, iq_receiver_config

__all__ = ["ESPARGOSIQApplication", "IQCaptureApplication", "IQController"]


class IQController(PyQt6.QtCore.QObject):
    """Qt/QML controls for a :class:`.Pool` and its IQ receiver.

    Exposes the pool's control surface as non-blocking QML slots for the
    shared drawer controls (``IQSettings.qml`` / ``ReftxToneSettings.qml``):
    blocking pool calls run on worker threads, rapid tone edits coalesce, and
    out-of-band state changes surface as Qt signals.
    """

    # Tone state changed OUTSIDE the GUI tone controls (sync/calibration
    # disable the tone); GUI reloads its switch/fields from the controller.
    configChanged = PyQt6.QtCore.pyqtSignal(object)
    configApplied = PyQt6.QtCore.pyqtSignal(dict)
    configSnapshot = PyQt6.QtCore.pyqtSignal(str)
    configFailed = PyQt6.QtCore.pyqtSignal(str)
    toneChanged = PyQt6.QtCore.pyqtSignal()
    calibrationChanged = PyQt6.QtCore.pyqtSignal()
    gainPhaseCompensationChanged = PyQt6.QtCore.pyqtSignal()
    referenceBusyChanged = PyQt6.QtCore.pyqtSignal()
    syncNeededChanged = PyQt6.QtCore.pyqtSignal()

    def __init__(
        self,
        pool: Pool,
        logger=None,
        parent=None,
        *,
        capture_config=None,
    ):
        super().__init__(parent)
        self.pool = pool
        self.receiver = pool.iq
        self._desired_capture = None if capture_config is None else dict(capture_config)
        self.logger = logger if logger is not None else logging.getLogger("pyespargos.iq_application")
        # GUI toggle: apply the fine calibration to displayed data
        self.apply_calibration = False
        self._correction_cache = {}
        # reference-tone control: coalescing worker so the controller RPC never
        # blocks the GUI thread and rapid slider/field edits collapse to the last
        self._tone_state_lock = threading.Lock()
        self._reference_io_lock = threading.Lock()
        self._tone_pending = None
        self._tone_busy = False
        self._reference_busy = False
        self._sync_needed = True
        self._calibration_error = ""
        self._config_state_lock = threading.Lock()
        self._config_pending = None
        self._config_worker = None
        self._reading_config = False
        self._io_workers = []
        self._closing = False
        self.receiver.add_stream_callback(self._observe_capture)
        self.receiver.add_callback(self._observe_capture)
        self.receiver.add_accumulation_callback(self._observe_capture)

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def boardCount(self):
        return len(self.pool.boards)

    @PyQt6.QtCore.pyqtProperty(bool, notify=referenceBusyChanged)
    def referenceBusy(self):
        return self._reference_busy

    @PyQt6.QtCore.pyqtProperty(str, notify=calibrationChanged)
    def calibrationError(self):
        return self._calibration_error

    @PyQt6.QtCore.pyqtProperty(bool, notify=syncNeededChanged)
    def syncNeeded(self):
        return self._sync_needed

    def _set_sync_needed(self, needed):
        if self._sync_needed != needed:
            self._sync_needed = needed
            self.syncNeededChanged.emit()

    def _observe_capture(self, capture):
        if not self._reference_busy and not self.receiver.calibration_applies_to(capture):
            self._set_sync_needed(True)

    def _check_calibration_config(self, config):
        center = self.receiver.calibration_center_frequency
        rate = self.receiver.calibration_sample_rate
        frequencies = [r.get("rf_freq_hz", center) for r in config.get("receivers", [])]
        frequencies.append(config.get("rf_freq_hz", center))
        capture_rate = rate
        if config.get("trigger", {}).get("mode") == "downsample" or "adc_decimation" in config:
            capture_rate = receive_sample_rate(config)
        elif "bluetooth_sample_rate_hz" in config:
            capture_rate = config["bluetooth_sample_rate_hz"]
        calibration = self.receiver.calibration
        if calibration is None or not np.all(np.isfinite(calibration.phase_offsets)) or any(f != center for f in frequencies) or capture_rate != rate:
            self._set_sync_needed(True)

    def _begin_reference(self):
        if self._reference_busy or self._closing:
            return False
        self._reference_busy = True
        self._set_sync_needed(True)
        with self._tone_state_lock:
            self._tone_pending = None
        self.referenceBusyChanged.emit()
        return True

    def _end_reference(self, succeeded):
        self._correction_cache = {}
        if succeeded:
            self._calibration_error = ""
        self._set_sync_needed(not succeeded)
        self._reference_busy = False
        self.calibrationChanged.emit()
        self.toneChanged.emit()
        self.referenceBusyChanged.emit()

    # ---- capture configuration ----

    @PyQt6.QtCore.pyqtSlot(str)
    def queue_config_json(self, config_json):
        """Coalesce interactive receiver edits without blocking video or QML."""
        try:
            config = json.loads(config_json)
            if not isinstance(config, dict):
                raise ValueError("Receiver configuration must be an object")
        except (ValueError, TypeError) as error:
            self.logger.error("Invalid receiver configuration: %s", error)
            return
        with self._config_state_lock:
            if self._closing or self._reference_busy:
                return
            # Top-level patches merge, while a trigger replaces the whole
            # trigger object so fields from an old mode cannot leak through.
            self._config_pending = {**(self._config_pending or {}), **config}
            if self._config_worker is None:
                self._config_worker = threading.Thread(target=self._drain_config, daemon=True)
                self._config_worker.start()

    def _drain_config(self):
        while True:
            with self._config_state_lock:
                config = self._config_pending
                self._config_pending = None
                if self._closing or config is None:
                    self._config_worker = None
                    return
            self._apply_config(config)

    def close(self):
        """Finish owned I/O workers before the application closes the pool."""
        with self._config_state_lock:
            self._closing = True
            self._config_pending = None
            worker = self._config_worker
            io_workers = tuple(self._io_workers)
        with self._tone_state_lock:
            self._tone_pending = None
        if worker is not None:
            worker.join()
        for worker in io_workers:
            worker.join()

    def _start_io_worker(self, target):
        with self._config_state_lock:
            if self._closing:
                return
            self._io_workers = [worker for worker in self._io_workers if worker.is_alive()]
            worker = threading.Thread(target=target, daemon=True)
            self._io_workers.append(worker)
            worker.start()

    @PyQt6.QtCore.pyqtSlot(str)
    def apply_config_json(self, config_json):
        if self._reference_busy or self._closing:
            return
        try:
            config = json.loads(config_json)
        except (ValueError, TypeError) as error:
            self.configFailed.emit(str(error))
            return
        self._apply_config(config)

    def _apply_config(self, config):
        try:
            # The IQ API accepts one patch for tuning, gain, path and trigger.
            # Apply coupled changes together so the sensors never briefly run
            # a new trigger on the previous path or sample rate.
            with self._reference_io_lock:
                if self._closing:
                    return
                self.apply_capture_patch(config)
            self._check_calibration_config(config)
            self._correction_cache.clear()
            self.configApplied.emit(config)
            self.configChanged.emit(config)
        except Exception as e:
            self.logger.error(f"set_iq_control failed: {e}")
            self.configFailed.emit(str(e))
            self.request_config()

    @PyQt6.QtCore.pyqtSlot()
    def request_config(self):
        """Refresh the RX drawer without blocking Qt on a board request."""
        with self._config_state_lock:
            if self._closing or self._reading_config:
                return
            self._reading_config = True

        def read():
            try:
                with self._reference_io_lock:
                    if self._closing or self._reference_busy:
                        return
                    config = self.receiver.get_config()
                self._check_calibration_config(config)
                self.configChanged.emit(config)
                self.configSnapshot.emit(json.dumps(config))
            except Exception as error:
                self.logger.warning("Could not read receiver settings: %s", error)
                self.configFailed.emit(str(error))
            finally:
                with self._config_state_lock:
                    self._reading_config = False

        self._start_io_worker(read)

    @PyQt6.QtCore.pyqtProperty(bool, notify=gainPhaseCompensationChanged)
    def gain_phase_compensation(self):
        return self.receiver.gain_phase_compensation

    @PyQt6.QtCore.pyqtSlot(bool)
    def set_gain_phase_compensation(self, enabled):
        enabled = bool(enabled)
        if enabled == self.receiver.gain_phase_compensation:
            return
        self.receiver.gain_phase_compensation = enabled
        self.gainPhaseCompensationChanged.emit()

    # ---- combined synchronization and calibration ----

    def apply_capture_patch(self, config):
        """Apply and remember a receiver edit while the caller holds the I/O lock."""
        self.receiver.apply_config(config)
        if self._desired_capture is not None:
            self._desired_capture.update(config)

    def ensure_capture_mode(self):
        """Called under the reference I/O lock before calibration or after reconnect."""
        if all(mode == "iq_rx" for board in self.pool.boards for mode in board.iq.get_config()["modes"]):
            return
        if self._desired_capture is None:
            raise RuntimeError("Receiver left IQ mode. Restart the IQ demo to restore its capture settings.")
        self.pool.configure(mode="iq_rx")
        self.receiver.apply_config(self._desired_capture)
        self._set_sync_needed(True)

    @PyQt6.QtCore.pyqtSlot()
    def sync_async(self):
        if not self._begin_reference():
            return

        # Finish edits accepted before the button press. Waiting on the GUI
        # thread would freeze video during a slow receiver write.
        with self._config_state_lock:
            pending_edits = self._config_worker

        def run():
            succeeded = False
            try:
                if pending_edits is not None:
                    pending_edits.join()
                with self._reference_io_lock:
                    if not self._closing:
                        self.ensure_capture_mode()
                        self.pool.calibrate()
                        succeeded = True
            except Exception as e:
                self._calibration_error = str(e)
                self.logger.error(f"sync/calibration failed: {e}")
            finally:
                self._end_reference(succeeded)

        self._start_io_worker(run)

    @PyQt6.QtCore.pyqtSlot(bool)
    def set_apply_calibration(self, enable):
        """GUI toggle: apply the fine calibration to displayed data (demos
        check this via :meth:`display_correction`)."""
        self.apply_calibration = bool(enable)

    def cal_correction(self, board_index, antenna_id, n=CHUNK_SAMPLES):
        """Per-fftshifted-bin correction for (board, firmware antenna id) at
        FFT size n, from the pool's stored calibration; None if not
        calibrated."""
        corrections = self._correction_cache.get(n)
        if corrections is None:
            corrections = self.receiver.cal_correction(n)
            if corrections is None:
                return None
            self._correction_cache[n] = corrections
        row, col = self.receiver.boards[board_index].revision.antenna_id_to_row_col(antenna_id)
        return corrections[board_index, row, col]

    def display_correction(self, board_index, antenna_id, n=CHUNK_SAMPLES):
        """Return a display correction while the current capture is calibrated."""
        if not self.apply_calibration or self.syncNeeded:
            return None
        return self.cal_correction(board_index, antenna_id, n)

    # ---- reference tone ----

    @PyQt6.QtCore.pyqtSlot(str, float, float)
    def reference_apply(self, mode, freq_mhz, atten_db):
        """Queue the latest Off/CW/noise request without blocking the GUI."""
        if mode not in ("off", "cw", "iq"):
            raise ValueError("Unknown reference mode")
        if self._reference_busy or self._closing:
            return
        with self._tone_state_lock:
            self._tone_pending = (mode, float(freq_mhz), float(atten_db))
            if self._tone_busy:
                return  # in-flight worker will pick up the latest
            self._tone_busy = True
        self._start_io_worker(self._tone_drain)

    def _tone_drain(self):
        while True:
            with self._tone_state_lock:
                request = self._tone_pending
                self._tone_pending = None
                if request is None:
                    self._tone_busy = False
                    return
            try:
                # Finish an in-flight tone write before reference acquisition;
                # discard queued edits once Sync or Calibrate has been clicked.
                with self._reference_io_lock:
                    if not self._reference_busy and not self._closing:
                        self._apply_tone_request(*request)
            except Exception as e:
                self.logger.error(f"reference control failed: {e}")
            finally:
                self.toneChanged.emit()

    def _apply_tone_request(self, mode, freq_mhz, atten_db):
        if mode == "off":
            self.receiver.set_reference_tone(enable=False)
            return
        backoff = int(round(max(0.0, min(atten_db, 20.0)) * 4))
        freq_hz = freq_mhz * 1e6
        if mode == "iq":
            if not 2310e6 <= freq_hz <= 2640e6:
                raise ValueError("Reference noise requires 2310–2640 MHz")
            self.receiver.master_board.iq.set_reference("iq", freq_hz, backoff)
        elif freq_hz <= iq_tone.TONE_MAX_HZ:
            self.receiver.set_reference_tone(freq_hz, backoff_qdb=backoff)
        else:
            cap, f_expected, approximate = self.receiver.place_highband_tone(freq_hz, backoff_qdb=backoff)
            if approximate:
                self.logger.info(f"high-band tone at ~{f_expected:.0f} MHz (cap {cap}, target {freq_hz/1e6:.0f}; wandering band, drifts ±10+ MHz, read exact frequency off the waterfall)")
            else:
                self.logger.info(f"high-band tone at ~{f_expected:.0f} MHz (cap {cap}, target {freq_hz/1e6:.0f})")

    @PyQt6.QtCore.pyqtSlot(result=str)
    def tone_get_json(self):
        try:
            return json.dumps(self.receiver.get_reference_tone())
        except Exception as e:
            self.logger.error(f"get_reference failed: {e}")
            return "{}"


class ESPARGOSIQApplication(ESPARGOSApplication):
    """Shared controller profile for IQ capture, transmission and mixed-role demos."""

    CONTROLLER_MODE = "iq"
    SENSOR_MODE = None


class IQCaptureApplication(ESPARGOSIQApplication):
    """
    ESPARGOS application whose sensing modality is raw synchronized IQ sampling.

    Provides board lifecycle and the IQ data path through :class:`.IQReceiver`
    (chunk clustering with completion callbacks, configuration fan-out,
    array-wide anchored time sync, reference tone, fine calibration). The
    pool's processing worker is started automatically, so cluster callbacks
    and any :class:`.IQBacklog` are live once the pool is up. Demos typically
    create an :class:`IQController` (once the pool exists, e.g. on
    ``initComplete``) and expose it to QML for the shared IQ drawer.
    """

    SENSOR_MODE = "iq_rx"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Ctrl+C must take the regular shutdown path, which returns the array
        # to WiFi mode. Python only notices the signal while it executes, hence
        # the idle timer.
        # A Python signal can run inside a Qt video-frame callback. Calling
        # quit() there synchronously emits aboutToQuit and can tear down the
        # camera while its FFmpeg socket notifier is still delivering a frame.
        signal.signal(signal.SIGINT, lambda *_: PyQt6.QtCore.QMetaObject.invokeMethod(self, "quit", PyQt6.QtCore.Qt.ConnectionType.QueuedConnection))
        self._interrupt_timer = PyQt6.QtCore.QTimer(self)
        self._interrupt_timer.timeout.connect(lambda: None)
        self._interrupt_timer.start(200)

    def _create_receiver(self, owner) -> IQReceiver:
        self.request_mode(owner)
        return owner.iq

    def _calibrate_pool(self, calibrate: bool, additional_calibrate_args: dict) -> bool:
        # IQ synchronization includes calibration. The demo starts that
        # operation through the same controller used by the Sync button.
        self.pool.start_processing()
        return True

    @staticmethod
    def restore_wifi(pool):
        from demos.wifi.common.application import ESPARGOSWiFiApplication

        ESPARGOSWiFiApplication.request_mode(pool)

    def create_iq_controller(self, *, capture_config=None) -> IQController:
        """Create the QML adapter for this application's pool."""
        controller = IQController(self.pool, logger=self.logger, parent=self, capture_config=capture_config)
        if not hasattr(self, "_iq_controllers"):
            self._iq_controllers = []
        self._iq_controllers.append(controller)
        return controller

    def onAboutToQuit(self):
        for controller in getattr(self, "_iq_controllers", []):
            controller.close()
        if getattr(self, "receiver", None) is not None:
            # No callback may outlive the Qt objects it updates. The common
            # application teardown stops the transport, but IQ applications
            # also own a separate cluster-processing worker.
            self.pool.stop_processing()
            # Hand the array back to WiFi/CSI mode on exit (the firmware
            # force-disables a running reference tone on that transition).
            try:
                self.restore_wifi(self.pool)
            except Exception as error:
                self.logger.warning("Could not restore WiFi mode: %s", error)
        super().onAboutToQuit()
