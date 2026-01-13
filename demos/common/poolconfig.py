#!/usr/bin/env python3

import PyQt6.QtCore

import logging
import json

import espargos.pool

class PoolConfigManager(PyQt6.QtCore.QObject):
	def __init__(self, pool : espargos.pool.Pool, parent=None):
		super().__init__(parent)
		self.logger = logging.getLogger("demo.poolconfig")
		self.pool = pool

		self.config = {
			"channel": 0,
			"secondary_channel": 0,
			"per_board_calibration": False,
			"show_calibration": False,
			"rf_switch": 0,
			"acquire_lltf_force": 0,
			"automatic_rx_gain": 1,
			"rx_gain": 32,
			"automatic_fft_gain": 1,
			"fft_gain": 32,
			"mac_filter_enable": 0,
			"mac_address": ""
		}

		# CSI acquire config -> UI fields
		try:
			csi_cfg = self.pool.get_csi_acquire_config()
		except Exception as e:
			raise RuntimeError("Failed to get CSI acquire config from pool") from e

		if isinstance(csi_cfg, dict) and "acquire_csi_force_lltf" in csi_cfg:
			self.config["acquire_lltf_force"] = 1 if bool(csi_cfg["acquire_csi_force_lltf"]) else 0

		# Gain settings -> UI fields
		try:
			gain = self.pool.get_gain_settings()
		except Exception as e:
			raise RuntimeError("Failed to get gain settings from pool") from e

		if isinstance(gain, dict):
			if "rx_gain_enable" in gain:
				self.config["automatic_rx_gain"] = 0 if bool(gain["rx_gain_enable"]) else 1
			if "rx_gain_value" in gain:
				self.config["rx_gain"] = int(gain["rx_gain_value"])
			if "fft_scale_enable" in gain:
				self.config["automatic_fft_gain"] = 0 if bool(gain["fft_scale_enable"]) else 1
			if "fft_scale_value" in gain:
				self.config["fft_gain"] = int(gain["fft_scale_value"])

		# RF switch config -> UI fields
		try:
			rf = self.pool.get_rfswitch()
		except Exception as e:
			raise RuntimeError("Failed to get RF switch config from pool") from e
		
		self.config["rf_switch"] = int(rf.value)

		# MAC filter -> UI fields
		try:
			mf = self.pool.get_mac_filter()
		except Exception as e:
			raise RuntimeError("Failed to get MAC filter config from pool") from e

		if isinstance(mf, dict):
			self.config["mac_filter_enable"] = 1 if bool(mf.get("enable", False)) else 0
			self.config["mac_address"] = str(mf.get("mac", "") or "")

		# WiFi config -> channel fields
		try:
			wc = self.pool.get_wificonf()
		except Exception as e:
			raise RuntimeError("Failed to get WiFi config (wificonf) from pool") from e

		if isinstance(wc, dict):
			if "channel-primary" in wc:
				self.config["channel"] = int(wc["channel-primary"])
			if "channel-secondary" in wc:
				self.config["secondary_channel"] = int(wc["channel-secondary"])

	@PyQt6.QtCore.pyqtSlot(str, result=bool)
	def action(self, action_name):
		if action_name == "calibrate":
			# TODO: Calibrate parameters
			self.pool.calibrate()
			return True
		raise ValueError(f"Unknown action: {action_name}")

	@PyQt6.QtCore.pyqtSlot(result=str)
	def get_config_json(self):
		return json.dumps(self.config)

	@PyQt6.QtCore.pyqtSlot(str)
	def set_config_json(self, config_json):
		config = json.loads(config_json)