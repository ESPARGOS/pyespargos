#!/usr/bin/env python

import pathlib
import sys
import time

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin

import argparse
import numpy as np
import PyQt6.QtCore

import espargos.util


class EspargosDemoCFOViewer(BacklogMixin, ESPARGOSApplication):
    updateCFOs = PyQt6.QtCore.pyqtSignal(float, list, list)
    maxAgeChanged = PyQt6.QtCore.pyqtSignal()
    minAntennasChanged = PyQt6.QtCore.pyqtSignal()
    channelConfigChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "max_age": 10.0,
        "min_antennas": None,
    }

    def __init__(self, argv):
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Show the latest CFO value for each antenna",
            add_help=False,
        )
        super().__init__(
            argv,
            argparse_parent=parser,
        )

        self.startTimestamp = time.time()
        self._channel_primary = None
        self._secondary_channel_mode = None

        self.initialize_pool(calibrate=False)
        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "cfo-viewer.qml",
        )

        self.pooldrawer.configManager().updateAppState.connect(self._on_pool_config_changed)
        self._update_channel_config_from_pooldrawer()

    def _on_update_app_state(self, newcfg):
        if "max_age" in newcfg:
            self.maxAgeChanged.emit()
        if "min_antennas" in newcfg:
            self._register_backlog_callback()
            self.minAntennasChanged.emit()

        super()._on_update_app_state(newcfg)

    def _on_pool_config_changed(self, delta):
        if "channel" in delta or "secondary_channel" in delta:
            self._update_channel_config_from_pooldrawer()

    def _update_channel_config_from_pooldrawer(self):
        cfg = self.pooldrawer.configManager()
        primary = cfg.get("channel")
        secondary_mode = cfg.get("secondary_channel")
        if primary != self._channel_primary or secondary_mode != self._secondary_channel_mode:
            self._channel_primary = primary
            self._secondary_channel_mode = secondary_mode
            self.channelConfigChanged.emit()

    @PyQt6.QtCore.pyqtSlot()
    def update(self):
        if not hasattr(self, "backlog") or not self.backlog.nonempty():
            return

        cfo_backlog, timestamp_backlog = self.backlog.get_multiple(("cfo", "host_timestamp"))
        latest_reception = ~np.isnan(cfo_backlog[-1])
        valid = np.sum(~np.isnan(cfo_backlog), axis=0)
        cfo_averaged = np.divide(
            np.nansum(cfo_backlog, axis=0),
            valid,
            out=np.full(cfo_backlog.shape[1:], np.nan, dtype=np.float32),
            where=valid > 0,
        )
        timestamp = timestamp_backlog[-1] - self.startTimestamp

        self.updateCFOs.emit(
            timestamp,
            np.array(cfo_averaged, dtype=np.float32, copy=True).flatten().tolist(),
            np.array(latest_reception, dtype=bool, copy=True).flatten().tolist(),
        )

    def _make_predicate(self):
        min_antennas = self._effective_min_antennas()

        def predicate(cluster):
            return np.sum(cluster.get_completion()) >= min_antennas

        return predicate

    def _effective_min_antennas(self):
        min_antennas = self.appconfig.get("min_antennas")
        if min_antennas is None:
            return int(np.prod(self.pool.get_shape()))
        return int(min_antennas)

    def _register_backlog_callback(self):
        if not hasattr(self, "pool") or not hasattr(self, "backlog"):
            return

        self.pool.callbacks = [callback for callback in self.pool.callbacks if getattr(callback.cb, "__self__", None) is not self.backlog or getattr(callback.cb, "__func__", None) is not self.backlog._on_new_csi.__func__]
        self.pool.add_csi_callback(self.backlog._on_new_csi, cb_predicate=self._make_predicate())

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=maxAgeChanged)
    def maxCSIAge(self):
        return self.appconfig.get("max_age")

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def sensorCount(self):
        return np.prod(self.pool.get_shape())

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=minAntennasChanged)
    def minAntennas(self):
        return self._effective_min_antennas()

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=channelConfigChanged)
    def cfoPpmScale(self):
        center_frequency = self._get_center_frequency_hz()
        if center_frequency is None or center_frequency <= 0:
            return 0.0
        return 1.0e6 / center_frequency

    def _get_center_frequency_hz(self):
        if self._channel_primary is None:
            return None

        secondary_channel = self._channel_primary
        if self._secondary_channel_mode == 1:
            secondary_channel = self._channel_primary + 4
        elif self._secondary_channel_mode == 2:
            secondary_channel = self._channel_primary - 4

        return espargos.util.get_center_frequency(self._channel_primary, secondary_channel)

    def onInitComplete(self):
        self._register_backlog_callback()


app = EspargosDemoCFOViewer(sys.argv)
app.initComplete.connect(app.onInitComplete)
sys.exit(app.exec())
