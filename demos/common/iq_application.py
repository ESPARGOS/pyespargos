#!/usr/bin/env python

"""IQ-sampling modality for the pyespargos demo application framework.

Requires the pyespargos repository root on ``sys.path`` (like all demos), as
it builds on the common demo framework in ``demos/common``.

All IQ mechanics live in :class:`.IQPool` (chunk clustering, configuration,
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
from espargos.iq_pool import IQCalibrationError, IQPool, iq_receiver_config

__all__ = ["ESPARGOSIQApplication", "IQController"]


class IQController(PyQt6.QtCore.QObject):
    """Qt/QML adapter around an :class:`.IQPool`.

    Exposes the pool's control surface as non-blocking QML slots for the
    shared drawer controls (``IQSettings.qml`` / ``ReftxToneSettings.qml``):
    blocking pool calls run on worker threads, rapid tone edits coalesce, and
    out-of-band state changes surface as Qt signals.
    """

    # Tone state changed OUTSIDE the GUI tone controls (sync/calibration
    # disable the tone); GUI reloads its switch/fields from the controller.
    toneChanged = PyQt6.QtCore.pyqtSignal()
    calibrationChanged = PyQt6.QtCore.pyqtSignal()
    gainPhaseCompensationChanged = PyQt6.QtCore.pyqtSignal()

    def __init__(
        self,
        pool: IQPool,
        logger=None,
        parent=None,
        cable_lengths=None,
        cable_velocity_factors=None,
    ):
        super().__init__(parent)
        self.pool = pool
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
        self._tone_pending = None
        self._tone_busy = False

    # ---- capture configuration ----

    @PyQt6.QtCore.pyqtSlot(str)
    def apply_config_json(self, config_json):
        try:
            self.pool.apply_config(json.loads(config_json))
        except Exception as e:
            self.logger.error(f"set_iq_control failed: {e}")

    @PyQt6.QtCore.pyqtSlot(result=str)
    def get_config_json(self):
        try:
            return json.dumps(self.pool.get_config())
        except Exception as e:
            self.logger.error(f"get_iq_control failed: {e}")
            return "{}"

    @PyQt6.QtCore.pyqtProperty(bool, notify=gainPhaseCompensationChanged)
    def gain_phase_compensation(self):
        return self.pool.gain_phase_compensation

    @PyQt6.QtCore.pyqtSlot(bool)
    def set_gain_phase_compensation(self, enabled):
        enabled = bool(enabled)
        if enabled == self.pool.gain_phase_compensation:
            return
        self.pool.gain_phase_compensation = enabled
        self.gainPhaseCompensationChanged.emit()

    # ---- coarse time sync ----

    @PyQt6.QtCore.pyqtSlot()
    def sync_async(self):
        def run():
            try:
                self.pool.sync()
            except Exception as e:
                self.logger.error(f"time sync failed: {e}")
            finally:
                self.toneChanged.emit()  # sync switches the reference tone off

        threading.Thread(target=run, daemon=True).start()

    # ---- fine time/phase calibration ----

    @PyQt6.QtCore.pyqtSlot()
    def calibrate_auto_async(self):
        """Run the pool's model-free broadband calibration at the current
        center frequency on a worker thread (any center works: the reference
        is swept across the captured band, WiFi-channel tones <= 2497 MHz and
        forced-VCO-cap tones above)."""

        def run():
            try:
                center = int(iq_receiver_config(self.pool.get_config()).get("rf_freq_hz", 2437000000))
            except Exception:
                center = 2437000000
            board_count = len(self.pool.boards)
            if board_count == 1:
                self.logger.info(f"calibrating (broadband, model-free) at {center/1e6:.0f} MHz")
            else:
                self.logger.info(f"calibrating coherent array ({board_count} boards) at {center/1e6:.0f} MHz")
            try:
                self.pool.calibrate(center_hz=center, **self._calibration_kwargs)
            except IQCalibrationError as e:
                self.logger.error(f"calibration failed: {e}")
            except Exception as e:
                self.logger.error(f"calibration error: {e}")
            finally:
                self._correction_cache = {}
                self.calibrationChanged.emit()
                self.toneChanged.emit()  # the sweep switches the tone off

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
            corrections = self.pool.cal_correction(n)
            if corrections is None:
                return None
            self._correction_cache[n] = corrections
        row, col = self.pool.boards[board_index].revision.antenna_id_to_row_col(antenna_id)
        return corrections[board_index, row, col]

    def display_correction(self, board_index, antenna_id, n=CHUNK_SAMPLES):
        """:meth:`cal_correction` gated by the apply-calibration GUI toggle —
        what demos should use in their display paths."""
        if not self.apply_calibration:
            return None
        return self.cal_correction(board_index, antenna_id, n)

    # ---- reference tone ----

    @PyQt6.QtCore.pyqtSlot(bool, float, float, int)
    def tone_apply(self, enable, freq_mhz, atten_db, cbw):
        """Drive the master's reference CW tone from the GUI. NON-BLOCKING: the
        request is handed to a worker thread and only the LATEST request is
        acted on (rapid slider/field edits coalesce), so the GUI never stalls
        on the controller RPC or the high-band cap placement. atten_db 0..20
        is TX attenuation (backoff). Up to TONE_MAX_HZ the exact WiFi-channel
        path is used; above it the deterministic forced-VCO-cap path."""
        with self._tone_state_lock:
            self._tone_pending = (bool(enable), float(freq_mhz), float(atten_db))
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
                self._apply_tone_request(*request)
            except Exception as e:
                self.logger.error(f"tone control failed: {e}")

    def _apply_tone_request(self, enable, freq_mhz, atten_db):
        if not enable:
            self.pool.set_reference_tone(enable=False)
            self.toneChanged.emit()
            return
        backoff = int(round(max(0.0, min(atten_db, 20.0)) * 4))
        freq_hz = freq_mhz * 1e6
        if freq_hz <= iq_tone.TONE_MAX_HZ:
            self.pool.set_reference_tone(freq_hz, backoff_qdb=backoff)
        else:
            cap, f_expected, approximate = self.pool.place_highband_tone(freq_hz, backoff_qdb=backoff)
            if approximate:
                self.logger.info(f"high-band tone at ~{f_expected:.0f} MHz (cap {cap}, target {freq_hz/1e6:.0f}; wandering band, drifts ±10+ MHz, read exact frequency off the waterfall)")
            else:
                self.logger.info(f"high-band tone at ~{f_expected:.0f} MHz (cap {cap}, target {freq_hz/1e6:.0f})")

    @PyQt6.QtCore.pyqtSlot(result=str)
    def tone_get_json(self):
        try:
            return json.dumps(self.pool.get_reference_tone())
        except Exception as e:
            self.logger.error(f"get_reftx_tone failed: {e}")
            return "{}"


class ESPARGOSIQApplication(ESPARGOSApplication):
    """
    ESPARGOS application whose sensing modality is raw synchronized IQ sampling.

    Provides board lifecycle and the IQ data path through :class:`.IQPool`
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

    def _create_pool(self, boards: list) -> IQPool:
        return IQPool(boards)

    def _calibrate_pool(self, calibrate: bool, additional_calibrate_args: dict) -> bool:
        # No start-up calibration: the IQ fine calibration is user-triggered
        # (drawer "Calibrate" button) since it is only valid within one sync
        # epoch anyway. Start the chunk-processing worker instead, so cluster
        # callbacks run from here on.
        self.pool.start_processing()
        return True

    def create_iq_controller(
        self,
        cable_lengths=None,
        cable_velocity_factors=None,
    ) -> IQController:
        """Create the QML adapter for this application's pool."""
        return IQController(
            self.pool,
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
            self.pool.restore_wifi()
        super().onAboutToQuit()
