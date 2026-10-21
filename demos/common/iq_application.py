#!/usr/bin/env python

"""IQ-sampling modality for the pyespargos demo application framework.

Requires the pyespargos repository root on ``sys.path`` (like all demos), as
it builds on the common demo framework in ``demos/common``.

All IQ mechanics live in :class:`.IQReceiver` (chunk clustering, configuration,
sync, reference tone, calibration); this module only adds the Qt layer:
:class:`IQController` adapts the pool to the QML drawer controls
(non-blocking slots, coalescing tone control, change signals), and
:class:`ESPARGOSIQApplication` slots the pool into the common demo framework.
"""

import json
import logging
import signal
import threading

import numpy as np
import PyQt6.QtCore

from demos.common.espargos_application import ESPARGOSApplication

from espargos import iq_tone
from espargos.iq_cluster import CHUNK_SAMPLES
from espargos.iq_packet import DECIM_TO_FS
from espargos.iq_receiver import IQReceiver, iq_receiver_config

__all__ = ["ESPARGOSIQApplication", "IQController"]


class IQController(PyQt6.QtCore.QObject):
    """Qt/QML adapter around an :class:`.IQReceiver`.

    Exposes the pool's control surface as non-blocking QML slots for the
    shared drawer controls (``IQSettings.qml`` / ``ReftxToneSettings.qml``):
    blocking pool calls run on worker threads, rapid tone edits coalesce, and
    out-of-band state changes surface as Qt signals.
    """

    # Tone state changed OUTSIDE the GUI tone controls (sync/calibration
    # disable the tone); GUI reloads its switch/fields from the controller.
    configChanged = PyQt6.QtCore.pyqtSignal(object)
    toneChanged = PyQt6.QtCore.pyqtSignal()
    calibrationChanged = PyQt6.QtCore.pyqtSignal()
    gainPhaseCompensationChanged = PyQt6.QtCore.pyqtSignal()
    referenceBusyChanged = PyQt6.QtCore.pyqtSignal()
    syncNeededChanged = PyQt6.QtCore.pyqtSignal()

    def __init__(
        self,
        pool: IQReceiver,
        logger=None,
        parent=None,
        cable_lengths=None,
        cable_velocity_factors=None,
    ):
        super().__init__(parent)
        self.receiver = pool
        self.logger = logger if logger is not None else logging.getLogger("pyespargos.iq_application")
        self._calibration_kwargs = {
            "cable_lengths": cable_lengths,
            "cable_velocity_factors": cable_velocity_factors,
        }
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
        self.receiver.add_callback(self._observe_capture)
        self.receiver.add_accumulation_callback(self._observe_capture)

    @PyQt6.QtCore.pyqtProperty(bool, notify=referenceBusyChanged)
    def referenceBusy(self):
        return self._reference_busy

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
        if self.receiver.calibration is None or any(f != center for f in frequencies) or DECIM_TO_FS.get(config.get("adc_decimation"), rate) != rate:
            self._set_sync_needed(True)

    def _begin_reference(self):
        if self._reference_busy:
            return False
        self._reference_busy = True
        self._set_sync_needed(True)
        with self._tone_state_lock:
            self._tone_pending = None
        self.referenceBusyChanged.emit()
        return True

    def _end_reference(self, succeeded):
        self._correction_cache = {}
        self._set_sync_needed(not succeeded)
        self._reference_busy = False
        self.calibrationChanged.emit()
        self.toneChanged.emit()
        self.referenceBusyChanged.emit()

    # ---- capture configuration ----

    @PyQt6.QtCore.pyqtSlot(str)
    def apply_config_json(self, config_json):
        if self._reference_busy:
            return
        try:
            config = json.loads(config_json)
            self.receiver.apply_config(config)
            self._check_calibration_config(config)
            self.configChanged.emit(config)
        except Exception as e:
            self.logger.error(f"set_iq_control failed: {e}")

    @PyQt6.QtCore.pyqtSlot(result=str)
    def get_config_json(self):
        try:
            config = self.receiver.get_config()
            self._check_calibration_config(config)
            self.configChanged.emit(config)
            return json.dumps(config)
        except Exception as e:
            self.logger.error(f"get_iq_control failed: {e}")
            return "{}"

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

    @PyQt6.QtCore.pyqtSlot()
    def sync_async(self):
        if not self._begin_reference():
            return

        def run():
            succeeded = False
            try:
                with self._reference_io_lock:
                    self.receiver.sync(**self._calibration_kwargs)
                    succeeded = True
            except Exception as e:
                self.logger.error(f"sync/calibration failed: {e}")
            finally:
                self._end_reference(succeeded)

        threading.Thread(target=run, daemon=True).start()

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
        """:meth:`cal_correction` gated by the apply-calibration GUI toggle —
        what demos should use in their display paths."""
        if not self.apply_calibration:
            return None
        return self.cal_correction(board_index, antenna_id, n)

    # ---- reference tone ----

    @PyQt6.QtCore.pyqtSlot(str, float, float)
    def reference_apply(self, mode, freq_mhz, atten_db):
        """Queue the latest Off/CW/noise request without blocking the GUI."""
        if mode not in ("off", "cw", "iq"):
            raise ValueError("Unknown reference mode")
        if self._reference_busy:
            return
        with self._tone_state_lock:
            self._tone_pending = (mode, float(freq_mhz), float(atten_db))
            if self._tone_busy:
                return  # in-flight worker will pick up the latest
            self._tone_busy = True
        threading.Thread(target=self._tone_drain, daemon=True).start()

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
                    if not self._reference_busy:
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Ctrl+C must take the regular shutdown path, which returns the array
        # to WiFi mode. Python only notices the signal while it executes, hence
        # the idle timer.
        signal.signal(signal.SIGINT, lambda *_: self.quit())
        self._interrupt_timer = PyQt6.QtCore.QTimer(self)
        self._interrupt_timer.timeout.connect(lambda: None)
        self._interrupt_timer.start(200)

    def _create_receiver(self, owner) -> IQReceiver:
        owner.configure(mode="iq_rx")
        return owner.iq

    def _calibrate_pool(self, calibrate: bool, additional_calibrate_args: dict) -> bool:
        # IQ synchronization includes calibration. The demo starts that
        # operation through the same controller used by the Sync button.
        self.pool.start_processing()
        return True

    def create_iq_controller(
        self,
        cable_lengths=None,
        cable_velocity_factors=None,
    ) -> IQController:
        """Create the QML adapter for this application's pool."""
        return IQController(
            self.receiver,
            logger=self.logger,
            parent=self,
            cable_lengths=cable_lengths,
            cable_velocity_factors=cable_velocity_factors,
        )

    def onAboutToQuit(self):
        if hasattr(self, "pool"):
            # No callback may outlive the Qt objects it updates. The common
            # application teardown stops the transport, but IQ applications
            # also own a separate cluster-processing worker.
            self.pool.stop_processing()
            # Hand the array back to WiFi/CSI mode on exit (the firmware
            # force-disables a running reference tone on that transition).
            self.receiver.restore_wifi()
        super().onAboutToQuit()
