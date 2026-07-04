#!/usr/bin/env python

import threading

import numpy as np
import PyQt6.QtCore

import espargos.constants
import espargos.radar

# Canonical radar configuration keys shared by all radar demos.
# Merge into a demo's DEFAULT_CONFIG (demos may add their own keys on top).
RADAR_CONFIG_DEFAULTS = {
    "tx_antenna": 3,  # "all" for multi-antenna TDM, or a pool-wide sensor index (single TX)
    "tx_format": "lltf",  # transmitted waveform: lltf / ht20 / ht40 / he20
    "period_ms": 5.0,  # transmit interval per antenna
    "tx_power": 80,  # wifi_tx_power_t value for 20 dBm
    "rfswitch_state": 2,
    "cfo_compensation": False,  # off by default for radar (shared TX/RX clock)
    "gain_calib_duration": 1.0,
    "tx_timestamp_offset_ns": 1085,
    "rx_array": 0,  # RX-side: which receiver array (board index) to process (bistatic)
}

# Uniform fixed gains written at startup / bringup to overwrite any leftover per-sensor gains
# (AGC is enabled, so these only act as the AGC starting point / fallback).
RADAR_DEFAULT_RX_GAIN_VALUE = 33
RADAR_DEFAULT_FFT_SCALE_VALUE = 20

# Radar keys that, when changed live, warrant re-running single-TX gain calibration.
_GAIN_AFFECTING_KEYS = {"tx_antenna", "tx_power", "rfswitch_state", "tx_format"}
# TX-relevant keys (everything except the RX-side selection) trigger a TX reconfigure.
_TX_KEYS = set(RADAR_CONFIG_DEFAULTS) - {"rx_array"}


class RadarControlMixin:
    """
    Generic radar TX control for ESPARGOS demos.

    Owns an :class:`espargos.radar.RadarSession` built from the shared radar
    app-config keys (:data:`RADAR_CONFIG_DEFAULTS`), applies it live when the user
    changes settings in the GUI, and runs single-TX gain calibration off the GUI
    thread. Multi-antenna (``tx_antenna == "all"``) stays on AGC — gain freezing is
    only valid with a single transmitting antenna.

    Mix in BEFORE ESPARGOSApplication and call :meth:`init_radar_control` from the
    demo's ``__init__`` (after ``super().__init__``). Register the demo's own CSI
    callback with :func:`espargos.radar.radar_completion_predicate`. Override the
    hooks below for demo-specific behavior:

    * :meth:`radar_gain_fallback` – gain dict applied if calibration collects nothing.
    * :meth:`on_radar_active` – called (off the GUI thread) when RX processing should
      start/stop; a demo pauses/clears its DSP here.
    """

    radarStatusChanged = PyQt6.QtCore.pyqtSignal()
    # Emitted once the pool is known so the TX-antenna / RX-array combo models can populate.
    radarOptionsChanged = PyQt6.QtCore.pyqtSignal()

    def init_radar_control(self):
        self._radar_status = "idle"
        self.radar_running = False
        self.gain_calibrating = False
        self.radar_session = None
        # Keep the RX CSI acquire config in sync with the RX preamble-format selector
        # (Common.GenericAppSettings). "auto" accepts every format; a specific format forces it.
        if hasattr(self, "preambleFormatChanged"):
            self.preambleFormatChanged.connect(self.apply_rx_acquire_config)

    def apply_rx_acquire_config(self):
        """Apply the CSI acquire config for the selected RX preamble format (RX-side, decoupled from TX)."""
        if getattr(self, "pool", None) is None:
            return
        self.pool.set_csi_acquire_config(espargos.radar.rx_acquire_config(self.genericconfig.get("preamble_format")))

    # ---- receiver array selection (bistatic) ----
    def num_boards(self) -> int:
        pool = getattr(self, "pool", None)
        return len(pool.boards) if pool is not None else 1

    def rx_boards(self) -> int:
        """Board index of the selected receiver array (single RX array)."""
        return int(self.appconfig.get("rx_array") or 0)

    def radar_rx_predicate(self, cluster):
        """cb_predicate accepting radar packets fully received by the selected RX array."""
        if not (cluster.is_radar() and cluster.has_radar_tx_report()):
            return False
        comp = np.asarray(cluster.get_completion())  # (boards, rows, cols)
        rx = self.rx_boards()
        board_comp = comp[rx] if 0 <= rx < comp.shape[0] else comp
        return np.sum(board_comp) >= board_comp.size - 1

    def refresh_radar_options(self):
        """Repopulate the TX-antenna / RX-array combo models (call once the pool exists)."""
        self.radarOptionsChanged.emit()

    @PyQt6.QtCore.pyqtProperty("QVariantList", notify=radarOptionsChanged)
    def txAntennas(self):
        """ComboBox model for the TX antenna selector: "All (TDM)" + every pool-wide antenna."""
        nboards = self.num_boards()
        per_board = espargos.constants.ANTENNAS_PER_BOARD
        options = [{"value": "all", "text": "All (TDM)"}]
        for idx in range(nboards * per_board):
            text = f"Array {idx // per_board} · Ant {idx % per_board}" if nboards > 1 else f"Antenna {idx}"
            options.append({"value": idx, "text": text})
        return options

    @PyQt6.QtCore.pyqtProperty("QVariantList", notify=radarOptionsChanged)
    def rxArrays(self):
        """ComboBox model for the RX array selector (one entry per receiver array/board)."""
        return [{"value": b, "text": f"Array {b}"} for b in range(self.num_boards())]

    # ---- status ----
    @PyQt6.QtCore.pyqtProperty(str, notify=radarStatusChanged)
    def radar_status(self):
        return self._radar_status

    def _set_radar_status(self, status):
        self._radar_status = status
        self.radarStatusChanged.emit()

    # ---- config ----
    def radar_is_single_tx(self) -> bool:
        return self.appconfig.get("tx_antenna") not in ("all", None)

    def radar_is_bistatic(self) -> bool:
        """True if the single TX antenna is on a different array than the selected RX array."""
        antenna = self.appconfig.get("tx_antenna")
        if antenna in ("all", None):
            return False
        return int(antenna) // espargos.constants.ANTENNAS_PER_BOARD != self.rx_boards()

    def reset_radar_gains(self):
        """
        Write one uniform fixed gain setting for every sensor and enable AGC (the default gain mode).

        On startup the boards are usually still left with the *differing* per-sensor gains from a
        previous run, which trips the pool drawer's gain-consistency readback before anything else
        runs. Writing a single uniform setting clears that. AGC is enabled (and is required by
        calibrate_gains anyway); the fixed values only act as the AGC starting point / fallback.
        """
        if getattr(self, "pool", None) is not None:
            self.pool.set_gain_settings(
                {
                    "rx_gain_enable": False,
                    "fft_scale_enable": False,
                    "rx_gain_value": RADAR_DEFAULT_RX_GAIN_VALUE,
                    "fft_scale_value": RADAR_DEFAULT_FFT_SCALE_VALUE,
                }
            )

    def radar_config_from_appconfig(self) -> espargos.radar.RadarConfig:
        antenna = self.appconfig.get("tx_antenna")
        tx_antennas = True if antenna in ("all", None) else int(antenna)
        return espargos.radar.RadarConfig(
            tx_antennas=tx_antennas,
            interval=float(self.appconfig.get("period_ms")) / 1e3,
            format=self.appconfig.get("tx_format"),
            tx_power=int(self.appconfig.get("tx_power")),
            rfswitch_state=int(self.appconfig.get("rfswitch_state")),
            cfo_compensation=bool(self.appconfig.get("cfo_compensation")),
            tx_timestamp_offset_ns=float(self.appconfig.get("tx_timestamp_offset_ns")),
        )

    # ---- hooks (override in the demo) ----
    def radar_gain_fallback(self):
        return None

    def on_radar_active(self, active: bool):
        pass

    # ---- lifecycle ----
    @PyQt6.QtCore.pyqtSlot()
    def start_radar(self):
        if self.pool.get_calibration() is None or self.gain_calibrating:
            return
        self.radar_session = espargos.radar.RadarSession(self.pool, self.radar_config_from_appconfig())
        self.radar_running = True
        self.gain_calibrating = True
        threading.Thread(target=self._radar_bringup, daemon=True).start()

    def _radar_bringup(self):
        """Configure the pool and (single-TX only) lock gains, off the GUI thread."""
        try:
            self.on_radar_active(False)
            self._set_radar_status("configuring")
            self.radar_session.configure()
            # Reset to AGC first. Freeze per-sensor gains only for a monostatic single TX (constant
            # per-packet RX power); multi-antenna TDM and bistatic setups (TX on a different array
            # than RX) keep AGC, which also avoids inconsistent per-sensor gains across arrays.
            self.reset_radar_gains()
            if self.radar_is_single_tx() and not self.radar_is_bistatic():
                self._set_radar_status("calibrating gain")
                self.radar_session.calibrate_gains(
                    duration=float(self.appconfig.get("gain_calib_duration")),
                    settle=0.5,
                    fallback=self.radar_gain_fallback(),
                    # Scope to the RX array so gain calibration works bistatically (whole pool is
                    # never complete when the transmitter is on a different array).
                    predicate=espargos.radar.radar_completion_predicate("all", rx_boards=self.rx_boards()),
                )
            if not self.radar_running:
                return
            self.on_radar_active(True)
            self._set_radar_status("receiving")
        finally:
            self.gain_calibrating = False
            if not self.radar_running:
                self._set_radar_status("idle")

    @PyQt6.QtCore.pyqtSlot()
    def stop_radar(self):
        self.radar_running = False
        self.on_radar_active(False)
        if self.radar_session is not None:
            self.radar_session.stop()
        if not self.gain_calibrating:
            self._set_radar_status("idle")

    def reconfigure_radar(self, newcfg: dict):
        """Apply changed radar settings live (called from _on_update_app_state)."""
        if not self.radar_running or self.radar_session is None:
            return
        self.radar_session.config = self.radar_config_from_appconfig()
        if any(k in newcfg for k in _GAIN_AFFECTING_KEYS) and self.radar_is_single_tx() and not self.radar_is_bistatic():
            # Rebuild schedule and re-lock gains off the GUI thread
            if not self.gain_calibrating:
                self.gain_calibrating = True
                threading.Thread(target=self._radar_bringup, daemon=True).start()
        else:
            # Light change (interval / cfo / multi-TX): just re-apply TX schedule + CFO
            self.radar_session.configure()

    def _on_update_app_state(self, newcfg):
        # rx_array is RX-only and doesn't affect the TX schedule, so it is excluded here.
        if any(k in newcfg for k in _TX_KEYS):
            self.reconfigure_radar(newcfg)
        super()._on_update_app_state(newcfg)
