import PyQt6.QtCore

from .config_manager import ConfigManager

class BacklogSettings(PyQt6.QtCore.QObject):
	DEFAULT_CONFIG = {
		"backlog": {
			"size": 20,
			"preamble": "lltf"
		}
	}

	def __init__(self, backlog, initial_cfg=None, parent=None):
		super().__init__(parent=parent)
		
		self.cfgman = ConfigManager(self.DEFAULT_CONFIG, parent=self)
		self.backlog = backlog

		# TODO...

	def onUpdateState(self, newcfg):
		# Apply new config to backlog
		if "size" in newcfg.get("backlog", {}):
			self.backlog.set_size(newcfg["backlog"]["size"])
		if "preamble" in newcfg.get("backlog", {}):
			self.backlog.set_preamble(newcfg["backlog"]["preamble"])