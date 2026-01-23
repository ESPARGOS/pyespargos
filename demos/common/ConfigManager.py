#!/usr/bin/env python3

import PyQt6.QtCore

import logging
import json

# A class deriving from ConfigManager must
# - define DEFAULT_CONFIG as a dict of config keys to default values
# - implement get_config() -> dict
# - implement set_config(newcfg: dict) -> None
# - optionally implement _action_{action_name}() methods for actions
# It may then
# - use self.logger for logging
# - use emitConfigChanged() to notify QML of config changes
# - use emitShowError(title: str, message: str) to notify QML of errors

class ConfigManager(PyQt6.QtCore.QObject):
	configChanged = PyQt6.QtCore.pyqtSignal(str)

	# QML hook (ConfigManager.qml listens via Connections.onShowError)
	showError = PyQt6.QtCore.pyqtSignal(str, str)

	def __init__(self, parent=None):
		super().__init__(parent)

		self.logger = logging.getLogger("demo.ConfigManager")
		self.config = self.DEFAULT_CONFIG.copy()

	def emitConfigChanged(self):
		self.configChanged.emit(json.dumps(self.config))

	def emitShowError(self, title: str, message: str):
		self.showError.emit(title, message)

	@PyQt6.QtCore.pyqtSlot(str, result=bool)
	def action(self, action_name):
		# Call method called _action_{action_name} if it exists
		method_name = f"_action_{action_name}"
		if hasattr(self, method_name):
			method = getattr(self, method_name)
			method()
			return True
		else:
			raise ValueError(f"Unknown action: {action_name}")

	@PyQt6.QtCore.pyqtSlot(result=str)
	def get_config_json(self):
		return json.dumps(self.get_config())

	@PyQt6.QtCore.pyqtSlot(str)
	def set_config_json(self, config_json):
		incoming = json.loads(config_json)
		if not isinstance(incoming, dict):
			raise ValueError("config_json must decode to an object")

		# Merge with current config
		newcfg = dict(self.config)

		for k, v in incoming.items():
			if k not in newcfg:
				self.logger.warning(f"Ignoring unknown config key: {k}")
				continue

			type_ = type(newcfg[k])
			if type_ is bool:
				# UI-only keys are always int (0/1), treat as bool01
				try:
					newcfg[k] = 1 if bool(int(v)) else 0
				except Exception:
					raise ValueError(f"Invalid bool value for {k}: {v}")
			elif type_ is int:
				try:
					newcfg[k] = int(v)
				except Exception:
					raise ValueError(f"Invalid int value for {k}: {v}")
			elif type_ is str:
				newcfg[k] = (str(v) or "").strip()
			else:
				newcfg[k] = v

		self.set_config(newcfg)