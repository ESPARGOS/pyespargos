#!/usr/bin/env python

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[3]))

from demos.wifi.common.application import ESPARGOSCSIApplication

import numpy as np
import espargos
import argparse
import time
import threading

import PyQt6.QtCore


class EspargosDemoSpeedtest(ESPARGOSCSIApplication):
    throughputChanged = PyQt6.QtCore.pyqtSignal()
    minAntennasChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "min_antennas": 1,
    }

    def __init__(self, argv):
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Measure CSI packet throughput",
            add_help=False,
        )
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(
            argv,
            argparse_parent=parser,
        )

        self._throughput = 0.0
        self._packet_count = 0
        self._last_reset = time.time()
        self._lock = threading.Lock()
        self._callback_handle = None

        self.initialize_pool(calibrate=not self.args.no_calib)

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "Main.qml",
        )

    def _on_update_app_state(self, newcfg):
        if "min_antennas" in newcfg:
            # Re-register the callback with the new predicate
            self._register_callback()
            self.minAntennasChanged.emit()

        super()._on_update_app_state(newcfg)

    def _make_predicate(self):
        min_antennas = self.appconfig.get("min_antennas")

        def predicate(cluster):
            # Minimum age so that we don't count packets that are still being acquired (and thus have incomplete CSI data)
            return np.sum(cluster.completion) >= min_antennas and cluster.age > 0.5

        return predicate

    def _register_callback(self):
        if self._callback_handle is None:
            self._callback_handle = self.receiver.add_callback(
                self._on_csi,
                callback_predicate=self._make_predicate(),
            )
        else:
            self._callback_handle = self.receiver.replace_callback(
                self._callback_handle,
                self._on_csi,
                callback_predicate=self._make_predicate(),
            )

    def _on_csi(self, clustered_csi):
        with self._lock:
            self._packet_count += 1

    @PyQt6.QtCore.pyqtSlot()
    def update(self):
        now = time.time()
        with self._lock:
            elapsed = now - self._last_reset
            if elapsed >= 1.0:
                self._throughput = self._packet_count / elapsed
                self._packet_count = 0
                self._last_reset = now
                self.throughputChanged.emit()

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=throughputChanged)
    def throughput(self):
        return self._throughput

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def totalAntennas(self):
        return int(np.prod(self.receiver.shape))

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=minAntennasChanged)
    def minAntennas(self):
        return self.appconfig.get("min_antennas")

    def onInitComplete(self):
        self._register_callback()
        self.pool.start_processing()

        self.poll_timer = PyQt6.QtCore.QTimer(self)
        self.poll_timer.timeout.connect(self.update)
        self.poll_timer.start(10)

    def initialize_pool(self, **kwargs):
        super().initialize_pool(**kwargs)
        self.initComplete.connect(self.onInitComplete)

    def onAboutToQuit(self):
        self.pool.stop_processing()
        if self._callback_handle is not None:
            self.receiver.remove_callback(self._callback_handle)
            self._callback_handle = None
        super().onAboutToQuit()


if __name__ == "__main__":
    app = EspargosDemoSpeedtest(sys.argv)
    sys.exit(app.exec())
