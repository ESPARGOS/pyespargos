import copy

import PyQt6.QtCore
import espargos

from .config_manager import ConfigManager


class BacklogSettings(PyQt6.QtCore.QObject):
    DEFAULT_CONFIG = {
        "size": 20,
        "fields": {"ht20": False, "ht40": False, "he20": False, "lltf": True},
        "filters": {"exclude_11b": True},
    }

    def __init__(self, force_config=None, parent=None):
        super().__init__(parent=parent)

        self.cfgman = ConfigManager(self.DEFAULT_CONFIG, parent=self)
        self.cfgman.updateAppState.connect(self.onUpdateAppState)
        self.force_config = force_config
        self.backlog = None
        self.exclude_11b_filter = espargos.Exclude11bFilter()

    def set_backlog(self, backlog):
        self.backlog = backlog

        # Update configuration with initial backlog settings
        if self.force_config:
            self.cfgman.force(self.force_config)
        else:
            self._read_config()

    def _read_config(self):
        self.cfgman.set(
            {
                "size": self.backlog.get_size(),
                "fields": {
                    "ht20": "ht20" in self.backlog.get_fields(),
                    "ht40": "ht40" in self.backlog.get_fields(),
                    "he20": "he20" in self.backlog.get_fields(),
                    "lltf": "lltf" in self.backlog.get_fields(),
                },
                "filters": {
                    "exclude_11b": self.exclude_11b_filter in self.backlog.get_filters(),
                },
            }
        )

    def onUpdateAppState(self, newcfg):
        if self.backlog is None:
            print("BacklogSettings: backlog not set before config update")
            self.cfgman.updateAppStateHandled.emit()
            return

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

        if "filters" in newcfg:
            filters_cfg = newcfg["filters"]
            if "exclude_11b" in filters_cfg:
                if bool(filters_cfg["exclude_11b"]):
                    self.backlog.add_filter(self.exclude_11b_filter)
                else:
                    self.backlog.remove_filter(self.exclude_11b_filter)

        self._read_config()
        self.cfgman.updateAppStateHandled.emit()

    def configManager(self):
        return self.cfgman
