#!/usr/bin/env python3

import PyQt6.QtCore

import logging
import json
import re
import threading

import espargos.pool
import espargos.csi

class PoolConfigManager(PyQt6.QtCore.QObject):
	configChangedJson = PyQt6.QtCore.pyqtSignal(str)
	# QML hook (ConfigManager.qml listens via Connections.onShowError)
	showError = PyQt6.QtCore.pyqtSignal(str, str)
	# Internal: schedule starting the QTimer on the QObject's thread
	_scheduleApplyTimer = PyQt6.QtCore.pyqtSignal()

	DEFAULT_CONFIG = {
		"channel": 13,
		"secondary_channel": 2,
		"per_board_calibration": False,
		"show_calibration": False,
		"rf_switch": 1,
		"acquire_lltf_force": 0,
		"automatic_rx_gain": 1,
		"rx_gain": 32,
		"automatic_fft_gain": 1,
		"fft_gain": 32,
		"mac_filter_enable": 0,
		"mac_address": "ff:ff:ff:ff:ff:ff",
	}

	# Parameters that exist only in the UI (not applied to the Pool/boards)
	UI_ONLY_KEYS = (
		"per_board_calibration",
		"show_calibration",
	)

	def __init__(self, pool : espargos.pool.Pool, parent=None):
		super().__init__(parent)
		self.logger = logging.getLogger("demo.poolconfig")
		self.pool = pool

		# Base defaults (also used as fallback on pool read errors)
		self.config = dict(self.DEFAULT_CONFIG)

		# Populate from pool (authoritative for non-UI fields)
		self.config.update(self._read_config_from_pool())

		# Async apply state
		self._pending_cfg: dict | None = None  # delta dict; newest wins per key
		self._apply_lock = threading.Lock()
		self._apply_in_flight = False
		self._apply_timer = PyQt6.QtCore.QTimer(self)
		self._apply_timer.setSingleShot(True)
		self._apply_timer.timeout.connect(self._async_apply)
		self._scheduleApplyTimer.connect(lambda: self._apply_timer.start(0))

	def _read_config_from_pool(self) -> dict:
		"""
		Read device-backed configuration from Pool and map to UI fields.
		Returns a partial config dict (does not include purely-UI fields).
		"""
		cfg_out: dict = {}

		# CSI acquire config -> UI fields
		csi_cfg = self.pool.get_csi_acquire_config()
		if isinstance(csi_cfg, dict) and "acquire_csi_force_lltf" in csi_cfg:
			cfg_out["acquire_lltf_force"] = 1 if bool(csi_cfg["acquire_csi_force_lltf"]) else 0

		# Gain settings -> UI fields
		gain = self.pool.get_gain_settings()
		if isinstance(gain, dict):
			if "rx_gain_enable" in gain:
				cfg_out["automatic_rx_gain"] = 0 if bool(gain["rx_gain_enable"]) else 1
			if "rx_gain_value" in gain:
				cfg_out["rx_gain"] = int(gain["rx_gain_value"])
			if "fft_scale_enable" in gain:
				cfg_out["automatic_fft_gain"] = 0 if bool(gain["fft_scale_enable"]) else 1
			if "fft_scale_value" in gain:
				cfg_out["fft_gain"] = int(gain["fft_scale_value"])

		# RF switch config -> UI fields
		rf = self.pool.get_rfswitch()
		cfg_out["rf_switch"] = int(rf.value)

		# MAC filter -> UI fields
		mf = self.pool.get_mac_filter()
		if isinstance(mf, dict):
			cfg_out["mac_filter_enable"] = 1 if bool(mf.get("enable", False)) else 0
			cfg_out["mac_address"] = str(mf.get("mac", "") or "")

		# WiFi config -> channel fields
		wc = self.pool.get_wificonf()
		if isinstance(wc, dict):
			if "channel-primary" in wc:
				cfg_out["channel"] = int(wc["channel-primary"])
			if "channel-secondary" in wc:
				cfg_out["secondary_channel"] = int(wc["channel-secondary"])

		return cfg_out

	def _apply_config_to_pool(self, delta: dict):
		"""
		Apply a *delta* config to the Pool (delta contains only keys to change).
		UI-only keys are ignored.
		"""
		if not delta:
			return

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
		if any(k in delta for k in ("automatic_rx_gain", "rx_gain", "automatic_fft_gain", "fft_gain")):
			# For partial gain updates, fill missing values from current UI state.
			automatic_rx_gain = int(delta.get("automatic_rx_gain", self.config.get("automatic_rx_gain", 1)))
			rx_gain = int(delta.get("rx_gain", self.config.get("rx_gain", 32)))
			automatic_fft_gain = int(delta.get("automatic_fft_gain", self.config.get("automatic_fft_gain", 1)))
			fft_gain = int(delta.get("fft_gain", self.config.get("fft_gain", 32)))

			gain = self.pool.get_gain_settings()
			if not isinstance(gain, dict):
				raise RuntimeError("pool.get_gain_settings() returned non-dict")
			gain = dict(gain)
			gain["rx_gain_enable"] = bool(0 if automatic_rx_gain else 1)
			gain["rx_gain_value"] = rx_gain
			gain["fft_scale_enable"] = bool(0 if automatic_fft_gain else 1)
			gain["fft_scale_value"] = fft_gain
			self.pool.set_gain_settings(gain)

		# MAC filter
		if "mac_filter_enable" in delta or "mac_address" in delta:
			mac_filter_enable = int(delta.get("mac_filter_enable", self.config.get("mac_filter_enable", 0)))
			mac_address = str(delta.get("mac_address", self.config.get("mac_address", "")) or "").strip()

			self.pool.set_mac_filter({
				"enable": bool(mac_filter_enable),
				"mac": mac_address
			})

	@PyQt6.QtCore.pyqtSlot(str, result=bool)
	def action(self, action_name):
		if action_name == "reset_config":
			try:
				# Enqueue defaults (device-backed keys only)
				with self._apply_lock:
					self._pending_cfg = {k: v for k, v in self.DEFAULT_CONFIG.items() if k not in self.UI_ONLY_KEYS}

				# Update UI-only immediately
				for k in self.UI_ONLY_KEYS:
					self.config[k] = int(self.DEFAULT_CONFIG[k])

				self.configChangedJson.emit(json.dumps(self.config))
				self._apply_timer.start(0)
				return True
			except Exception as e:
				raise RuntimeError("Failed to reset pool configuration to defaults") from e
		elif action_name == "calibrate":
			# TODO: Calibrate parameters
			self.pool.calibrate(per_board=False, duration=1, run_in_thread=False)
			return True
		raise ValueError(f"Unknown action: {action_name}")

	@PyQt6.QtCore.pyqtSlot(result=str)
	def get_config_json(self):
		self.config.update(self._read_config_from_pool())
		return json.dumps(self.config)

	@PyQt6.QtCore.pyqtSlot(str)
	def set_config_json(self, config_json):
		incoming = json.loads(config_json)
		if not isinstance(incoming, dict):
			raise ValueError("config_json must decode to an object")

		# Merge with current config; ignore unknown keys (forward-compat)
		newcfg = dict(self.config)
		for k, v in incoming.items():
			if k in newcfg:
				newcfg[k] = v

		def as_int(v, *, minv=None, maxv=None):
			iv = int(v)
			if minv is not None and iv < minv:
				raise ValueError(f"value {iv} < {minv}")
			if maxv is not None and iv > maxv:
				raise ValueError(f"value {iv} > {maxv}")
			return iv

		def as_bool01(v):
			return 1 if bool(int(v)) else 0

		# Canonicalize / validate
		newcfg["channel"] = as_int(newcfg["channel"], minv=1, maxv=14)
		newcfg["secondary_channel"] = as_int(newcfg["secondary_channel"], minv=0, maxv=14)
		newcfg["per_board_calibration"] = as_bool01(newcfg["per_board_calibration"])
		newcfg["show_calibration"] = as_bool01(newcfg["show_calibration"])
		newcfg["rf_switch"] = as_int(newcfg["rf_switch"], minv=0)
		newcfg["acquire_lltf_force"] = as_bool01(newcfg["acquire_lltf_force"])
		newcfg["automatic_rx_gain"] = as_bool01(newcfg["automatic_rx_gain"])
		newcfg["rx_gain"] = as_int(newcfg["rx_gain"], minv=0, maxv=127)
		newcfg["automatic_fft_gain"] = as_bool01(newcfg["automatic_fft_gain"])
		newcfg["fft_gain"] = as_int(newcfg["fft_gain"], minv=0, maxv=127)
		newcfg["mac_filter_enable"] = as_bool01(newcfg["mac_filter_enable"])
		newcfg["mac_address"] = (str(newcfg["mac_address"] or "")).strip()

		if newcfg["mac_address"]:
			if not re.fullmatch(r"(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}", newcfg["mac_address"]):
				raise ValueError("mac_address must be in format 00:11:22:33:44:55")

		# Update UI-only fields immediately
		for k in self.UI_ONLY_KEYS:
			self.config[k] = int(newcfg[k])

		# Notify UI immediately for UI-only changes / optimistic state
		self.configChangedJson.emit(json.dumps(self.config))

		# Queue device-backed changes as *delta* (newest wins per key)
		delta: dict = {}
		for k, v in newcfg.items():
			if k in self.UI_ONLY_KEYS:
				continue
			if self.config.get(k) != v:
				delta[k] = v

		# (keep as-is, but ensure delta only contains device-backed keys)
		if delta:
			with self._apply_lock:
				if self._pending_cfg is None:
					self._pending_cfg = {}
				self._pending_cfg.update(delta)
			self._apply_timer.start(0)

	def _async_apply(self):
		# Ensure only one background applier runs at a time; coalesce pending updates.
		with self._apply_lock:
			if self._apply_in_flight:
				return
			if not self._pending_cfg:
				return
			delta_snapshot = dict(self._pending_cfg)
			self._pending_cfg = None
			self._apply_in_flight = True

		def worker(delta: dict):
			try:
				self._apply_config_to_pool(delta)
				readback = self._read_config_from_pool()

				# Keep UI-only keys as-is.
				with self._apply_lock:
					ui_only = {k: self.config.get(k) for k in self.UI_ONLY_KEYS}
					self.config.update(readback)
					self.config.update(ui_only)

				# Emit from worker thread; delivery to QML will be queued.
				self.configChangedJson.emit(json.dumps(self.config))

			except Exception as e:
				err_str = str(e)
				self.showError.emit("Failed to apply configuration", err_str)

			finally:
				with self._apply_lock:
					self._apply_in_flight = False
					has_more = bool(self._pending_cfg)

				# Trigger next run if more deltas arrived meanwhile (must be on QObject thread)
				if has_more:
					self._scheduleApplyTimer.emit()

		threading.Thread(target=worker, args=(delta_snapshot,), daemon=True).start()