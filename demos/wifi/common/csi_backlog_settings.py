import logging

import PyQt6.QtCore
import espargos

from demos.common.config_manager import ConfigManager


class CSIBacklogSettings(PyQt6.QtCore.QObject):
    def __init__(self, config, parent=None):
        super().__init__(parent=parent)
        self.cfgman = ConfigManager(config, parent=self)
        self.cfgman.updateAppState.connect(self.onUpdateAppState)
        self.backlog = None
        self.exclude_11b_filter = espargos.Exclude11bFilter()

    def set_backlog(self, backlog):
        self.backlog = backlog

        if self.cfgman.get("filters", "exclude_11b"):
            backlog.add_filter(self.exclude_11b_filter)
        self._read_config()

    def _read_config(self):
        fields = self.backlog.fields
        self.cfgman.set(
            {
                "size": self.backlog.size,
                "fields": {name: name in fields for name in self.cfgman.get("fields")},
                "filters": {
                    "exclude_11b": self.exclude_11b_filter in self.backlog.filters,
                },
            }
        )

    def onUpdateAppState(self, newcfg):
        if self.backlog is None:
            logging.getLogger("demo.CSIBacklogSettings").warning("Backlog not set before config update")
            self.cfgman.updateAppStateHandled.emit()
            return

        if "size" in newcfg and newcfg["size"] != self.backlog.size:
            self.backlog.size = newcfg["size"]

        if "fields" in newcfg:
            fields = self.backlog.fields
            for field, enabled in newcfg["fields"].items():
                if enabled and field in self.backlog.available_fields:
                    fields.add(field)
                else:
                    fields.discard(field)
            if fields != self.backlog.fields:
                self.backlog.fields = fields

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
