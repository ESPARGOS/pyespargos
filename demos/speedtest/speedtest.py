#!/usr/bin/env python

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication

import numpy as np
import espargos
import argparse
import time
import threading

import PyQt6.QtCore


class EspargosDemoSpeedtest(ESPARGOSApplication):
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
        self._pool_runner_running = threading.Event()
        self._pool_runner_thread = None

        self.initialize_pool(calibrate=not self.args.no_calib)

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "speedtest-ui.qml",
        )

    def _on_update_app_state(self, newcfg):
        if "min_antennas" in newcfg:
            # Re-register the callback with the new predicate
            self._register_callback()
            self.minAntennasChanged.emit()

        super()._on_update_app_state(newcfg)

    def _make_predicate(self):
        min_antennas = self.appconfig.get("min_antennas")

        def predicate(completion, age):
            # Minimum age so that we don't count packets that are still being acquired (and thus have incomplete CSI data)
            return np.sum(completion) >= min_antennas and age > 0.5

        return predicate

    def _register_callback(self):
        # Clear existing callbacks and re-register with the new predicate
        self.pool.callbacks.clear()
        self.pool.add_csi_callback(self._on_csi, cb_predicate=self._make_predicate())

    def _on_csi(self, clustered_csi):
        with self._lock:
            self._packet_count += 1

    def _pool_runner(self):
        while self._pool_runner_running.is_set():
            self.pool.run()

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
        return int(np.prod(self.pool.get_shape()))

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=minAntennasChanged)
    def minAntennas(self):
        return self.appconfig.get("min_antennas")

    def onInitComplete(self):
        self._register_callback()
        self._pool_runner_running.set()
        self._pool_runner_thread = threading.Thread(target=self._pool_runner, daemon=True)
        self._pool_runner_thread.start()

        self.poll_timer = PyQt6.QtCore.QTimer(self)
        self.poll_timer.timeout.connect(self.update)
        self.poll_timer.start(10)

    def initialize_pool(self, **kwargs):
        super().initialize_pool(**kwargs)
        self.initComplete.connect(self.onInitComplete)

    def onAboutToQuit(self):
        self._pool_runner_running.clear()
        if self._pool_runner_thread is not None:
            self._pool_runner_thread.join(timeout=1.0)
        super().onAboutToQuit()


app = EspargosDemoSpeedtest(sys.argv)
sys.exit(app.exec())
