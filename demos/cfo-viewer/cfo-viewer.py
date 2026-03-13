#!/usr/bin/env python

import pathlib
import sys
import threading
import time

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin

import argparse
import numpy as np
import PyQt6.QtCore

import espargos.util


class EspargosDemoCFOViewer(BacklogMixin, ESPARGOSApplication):
    updateCFOs = PyQt6.QtCore.pyqtSignal(float, list)
    maxAgeChanged = PyQt6.QtCore.pyqtSignal()
    channelConfigChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "max_age": 10.0,
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

        self._pool_runner_running = threading.Event()
        self._pool_runner_thread = None
        self.startTimestamp = time.time()
        self._channel_primary = None
        self._secondary_channel_mode = None

        self.initialize_pool(calibrate=False)
        self.initComplete.connect(self._on_init_complete)
        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "cfo-viewer.qml",
        )

        self.pooldrawer.configManager().updateAppState.connect(self._on_pool_config_changed)
        self._update_channel_config_from_pooldrawer()

    def _on_update_app_state(self, newcfg):
        if "max_age" in newcfg:
            self.maxAgeChanged.emit()

        super()._on_update_app_state(newcfg)

    def _on_init_complete(self):
        self._pool_runner_running.set()
        self._pool_runner_thread = threading.Thread(target=self._pool_runner, daemon=True)
        self._pool_runner_thread.start()

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

    def _pool_runner(self):
        while self._pool_runner_running.is_set():
            self.pool.run()

    @PyQt6.QtCore.pyqtSlot()
    def update(self):
        if not hasattr(self, "backlog") or not self.backlog.nonempty():
            return

        cfo_backlog, timestamp_backlog = self.backlog.get_multiple(("cfo", "host_timestamp"))
        valid = np.sum(~np.isnan(cfo_backlog), axis=0)
        cfo_averaged = np.divide(
            np.nansum(cfo_backlog, axis=0),
            valid,
            out=np.full(cfo_backlog.shape[1:], np.nan, dtype=np.float32),
            where=valid > 0,
        )
        timestamp = timestamp_backlog[-1] - self.startTimestamp

        self.updateCFOs.emit(timestamp, np.array(cfo_averaged, dtype=np.float32, copy=True).flatten().tolist())

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=maxAgeChanged)
    def maxCSIAge(self):
        return self.appconfig.get("max_age")

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def sensorCount(self):
        return np.prod(self.pool.get_shape())

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

    def onAboutToQuit(self):
        self._pool_runner_running.clear()
        if self._pool_runner_thread is not None:
            self._pool_runner_thread.join(timeout=1.0)
        super().onAboutToQuit()


app = EspargosDemoCFOViewer(sys.argv)
sys.exit(app.exec())
