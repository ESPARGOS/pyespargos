#!/usr/bin/env python3

import PyQt6.QtCore

import threading
import re

import espargos.pool
import espargos.csi

from .config_manager import ConfigManager

class PoolDrawer(PyQt6.QtCore.QObject):
    DEFAULT_CONFIG = {
        "channel": 13,
        "secondary_channel": 2,
        "calibration": {
            "per_board": False,
            "show_csi": False,
            "duration": 1.0
        },
        "rf_switch": 2,
        "acquire_lltf_force": False,
        "rx_gain": {
            "automatic": True,
            "value": 32,
        },
        "fft_gain": {
            "automatic": True,
            "value": 32,
        },
        "mac_filter": {
            "enable": False,
            "mac_address": "ff:ff:ff:ff:ff:ff",
        },
    }

    # Init complete signal
    initComplete = PyQt6.QtCore.pyqtSignal()

    def __init__(self, pool : espargos.pool.Pool, force_config = None, parent = None):
        # Note that the current pool config is authoritative, the default config is just for UI initialization
        # However, if force_config is given, it takes precedence
        super().__init__(parent=parent)
        self.cfgman = ConfigManager(self.DEFAULT_CONFIG, self.DEFAULT_CONFIG, parent=self)
        self.pool = pool

        # Connect to UI changes
        self.cfgman.updateAppState.connect(self._write_config_to_pool)

        # Two initialization options:
        # * If force_config is given, apply it to both UI and Pool (config is authoritative)
        # * Otherwise, read current Pool config from devices and set UI accordingly (devices are authoritative)
        # Apply later
        def apply_initial_config():
            if force_config:
                self.cfgman.forceConfigApplied.connect(self.initComplete)
                self.cfgman.force(force_config)
            else:
                self.initComplete.emit()
                self.cfgman.set(self._read_config_from_pool())

        PyQt6.QtCore.QTimer.singleShot(0, apply_initial_config)

        # Connect actions
        self.cfgman.action.connect(lambda action_name: {
            "reset_config": self._action_reset_config,
            "reload_config": self._action_reload_config,
            "calibrate": self._action_calibrate,
        }.get(action_name, lambda: None)())

        self.calibration_running = False

    def configManager(self):
        return self.cfgman

    def _read_config_from_pool(self) -> dict:
        """
        Read device-backed configuration from Pool and map to UI fields.
        Returns a partial config dict (does not include purely-UI fields).
        """
        cfg_out: dict = {}

        # CSI acquire config -> UI fields
        csi_cfg = self.pool.get_csi_acquire_config()
        if isinstance(csi_cfg, dict) and "acquire_csi_force_lltf" in csi_cfg:
            cfg_out["acquire_lltf_force"] = bool(csi_cfg["acquire_csi_force_lltf"])

        # Gain settings -> UI fields
        gain = self.pool.get_gain_settings()
        if isinstance(gain, dict):
            rx_gain_cfg = {}
            fft_gain_cfg = {}
            if "rx_gain_enable" in gain:
                rx_gain_cfg["automatic"] = False if bool(gain["rx_gain_enable"]) else True
            if "rx_gain_value" in gain:
                rx_gain_cfg["value"] = int(gain["rx_gain_value"])
            if "fft_scale_enable" in gain:
                fft_gain_cfg["automatic"] = False if bool(gain["fft_scale_enable"]) else True
            if "fft_scale_value" in gain:
                fft_gain_cfg["value"] = int(gain["fft_scale_value"])
            if rx_gain_cfg:
                cfg_out["rx_gain"] = rx_gain_cfg
            if fft_gain_cfg:
                cfg_out["fft_gain"] = fft_gain_cfg

        # RF switch config -> UI fields
        rf = self.pool.get_rfswitch()
        cfg_out["rf_switch"] = int(rf.value)

        # MAC filter -> UI fields
        mf = self.pool.get_mac_filter()
        if isinstance(mf, dict):
            cfg_out["mac_filter"] = {
                "enable": bool(mf.get("enable", False)),
                "mac_address": str(mf.get("mac", "") or "")
            }

        # WiFi config -> channel fields
        wc = self.pool.get_wificonf()
        if isinstance(wc, dict):
            if "channel-primary" in wc:
                cfg_out["channel"] = int(wc["channel-primary"])
            if "channel-secondary" in wc:
                cfg_out["secondary_channel"] = int(wc["channel-secondary"])

        return cfg_out

    def _write_config_to_pool(self, delta: dict):
        def worker(delta: dict):
            """
            Apply a *delta* config to the Pool (delta contains only keys to change).
            UI-only keys are ignored.
            """
            try:
                # Validate mac_address format if present
                mac_filter_delta = delta.get("mac_filter") if isinstance(delta.get("mac_filter"), dict) else {}
                if mac_filter_delta.get("mac_address"):
                    if not re.fullmatch(r"(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}", mac_filter_delta["mac_address"]):
                        raise ValueError("mac_address must be in format 00:11:22:33:44:55")

                # WiFi channels
                if "channel" in delta or "secondary_channel" in delta:
                    wc = self.pool.get_wificonf()
                    if not isinstance(wc, dict):
                        raise RuntimeError("pool.get_wificonf() returned non-dict")
                    wc = dict(wc)
                    if "channel" in delta:
                        wc["channel-primary"] = int(delta["channel"])
                    if "secondary_channel" in delta:
                        wc["channel-secondary"] = int(delta["secondary_channel"])
                    self.pool.set_wificonf(wc)

                # RF switch
                if "rf_switch" in delta:
                    self.pool.set_rfswitch(espargos.csi.rfswitch_state_t(int(delta["rf_switch"])))

                # CSI acquire config
                if "acquire_lltf_force" in delta:
                    cfg = self.pool.get_csi_acquire_config()
                    if not isinstance(cfg, dict):
                        raise RuntimeError("pool.get_csi_acquire_config() returned non-dict")
                    cfg = dict(cfg)
                    cfg["acquire_csi_force_lltf"] = bool(int(delta["acquire_lltf_force"]))
                    self.pool.set_csi_acquire_config(cfg)

                # Gains
                if "rx_gain" in delta or "fft_gain" in delta:
                    gain_settings = dict()

                    if "rx_gain" in delta:
                        rx_gain_delta = delta["rx_gain"]
                        if "automatic" in rx_gain_delta:
                            gain_settings["rx_gain_enable"] = not bool(rx_gain_delta["automatic"])
                        if "value" in rx_gain_delta:
                            gain_settings["rx_gain_value"] = int(rx_gain_delta["value"])

                    if "fft_gain" in delta:
                        fft_gain_delta = delta["fft_gain"]
                        if "automatic" in fft_gain_delta:
                            gain_settings["fft_scale_enable"] = not bool(fft_gain_delta["automatic"])
                        if "value" in fft_gain_delta:
                            gain_settings["fft_scale_value"] = int(fft_gain_delta["value"])

                    # Partial updates are also allowed
                    self.pool.set_gain_settings(gain_settings)

                # MAC filter
                if "mac_filter" in delta:
                    mac_filter = dict()

                    mf_delta = delta["mac_filter"]
                    if "enable" in mf_delta:
                        mac_filter["enable"] = bool(mf_delta["enable"])
                    if "mac_address" in mf_delta:
                        mac_filter["mac"] = str(mf_delta["mac_address"])

                    self.pool.set_mac_filter(mac_filter)
            except Exception as e:
                err_str = str(e)
                self.cfgman.emitShowError("Failed to apply configuration", err_str)

            try:
                # Read back device-backed config to sync UI state
                self.cfgman.set(self._read_config_from_pool())
            except Exception as e:
                err_str = str(e)
                self.cfgman.emitShowError("Failed to read back configuration", err_str)
                
            # Let configmanager know we're done
            self.cfgman.updateAppStateHandled.emit()

        threading.Thread(target=worker, args=(delta,), daemon=True).start()

    def _action_reset_config(self):
        self._write_config_to_pool(self.DEFAULT_CONFIG)

    def _action_reload_config(self):
        self.cfgman.set(self._read_config_from_pool())

    def _action_calibrate(self):
        if self.calibration_running:
            return  # Avoid multiple concurrent calibrations

        self.calibration_running = True
        duration = self.cfgman.get("calibration", "duration")

        def _calibrate_thread():
            self.pool.calibrate(per_board=False, duration=duration, run_in_thread=False)
            self.calibration_running = False

        # Perform calibration in separate thread to avoid blocking UI
        threading.Thread(target=_calibrate_thread, daemon=True).start()