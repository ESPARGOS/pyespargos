import copy

import PyQt6.QtCore

from .config_manager import ConfigManager


class BacklogSettings(PyQt6.QtCore.QObject):
    DEFAULT_CONFIG = {"backlog": {"size": 20, "fields": {"ht20": False, "ht40": False, "lltf": True}}}

    def __init__(self, force_config=None, parent=None):
        super().__init__(parent=parent)

        self.cfgman = ConfigManager(self.DEFAULT_CONFIG, parent=self)
        self.cfgman.updateAppState.connect(self.onUpdateAppState)

        self.force_config = force_config

    def set_backlog(self, backlog):
        # Backlog is usually created after BacklogSettings (which needs to be ready for QML initialization),
        # so we provide a setter for it here.
        self.backlog = backlog

        # Update configuration with initial backlog settings
        if self.force_config:
            self.cfgman.force(self.force_config)
        else:
            self._read_config()

    def _read_config(self) -> dict:
        self.cfgman.set(
            {
                "size": self.backlog.get_size(),
                "fields": {
                    "ht20": "ht20" in self.backlog.get_fields(),
                    "ht40": "ht40" in self.backlog.get_fields(),
                    "lltf": "lltf" in self.backlog.get_fields(),
                },
            }
        )

    def onUpdateAppState(self, newcfg):
        if not hasattr(self, "backlog"):
            print("BacklogSettings: backlog not set before config update")
            self.cfgman.updateAppStateHandled.emit()
            return

        # Apply new config to backlog
        if "size" in newcfg:
            self.backlog.set_size(newcfg["size"])

        if "fields" in newcfg:
            new_fields = copy.deepcopy(self.backlog.get_fields())
            for field, enabled in newcfg["fields"].items():
                if enabled and field not in new_fields:
                    new_fields.add(field)
                elif not enabled and field in new_fields:
                    new_fields.remove(field)
            self.backlog.set_fields(new_fields)

        # Always read back the applied config
        self._read_config()

        self.cfgman.updateAppStateHandled.emit()

    def configManager(self):
        return self.cfgman
