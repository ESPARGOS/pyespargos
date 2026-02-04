#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, SingleCSIFormatMixin, ConfigManager

import numpy as np
import espargos
import espargos.constants
import argparse
import time

import PyQt6.QtCore


class EspargosDemoTDOAOverTime(BacklogMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    updateTDOAs = PyQt6.QtCore.pyqtSignal(float, list)
    maxAgeChanged = PyQt6.QtCore.pyqtSignal()
    averageChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "max_age": 10.0,
        "algorithm": "phase_slope",  # "phase_slope", "music", "unwrap"
        "average": False,
    }

    def __init__(self, argv):
        # Parse command line arguments
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Show time difference of arrival over time (single board)",
            add_help=False,
        )
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(
            argv,
            argparse_parent=parser,
        )

        # Set up ESPARGOS pool and backlog
        self.initialize_pool(calibrate=not self.args.no_calib)

        # App configuration manager
        self.appconfig = ConfigManager(self.DEFAULT_CONFIG, parent=self)
        self.appconfig.updateAppState.connect(self._on_update_app_state)

        # Apply optional YAML config to pool/demo config managers
        self.appconfig.set(self.get_initial_config("app", default={}))

        self.startTimestamp = time.time()

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "tdoas-over-time-ui.qml",
            {
                "appconfig": self.appconfig,
            },
        )

    def _on_update_app_state(self, newcfg):
        if "max_age" in newcfg:
            self.maxAgeChanged.emit()

        if "average" in newcfg:
            self.averageChanged.emit()

        self.appconfig.updateAppStateHandled.emit()

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=maxAgeChanged)
    def maxCSIAge(self):
        return self.appconfig.get("max_age")

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=averageChanged)
    def sensorCount(self):
        return np.prod(self.pool.get_shape()) if not self.appconfig.get("average") else self.pool.get_shape()[0]

    @PyQt6.QtCore.pyqtSlot()
    def update(self):
        if (result := self.get_backlog_csi("host_timestamp")) is None:
            return

        csi_backlog, timestamp_backlog = result
        mean_rx_timestamp = timestamp_backlog[-1] - self.startTimestamp

        # Do interpolation "by_array" due to Doppler (destroys TDoA for moving targets otherwise)
        csi_interp = espargos.util.csi_interp_iterative_by_array(csi_backlog, iterations=5)

        algorithm = self.appconfig.get("algorithm")
        do_average = self.appconfig.get("average")

        if algorithm == "music":
            tdoas_ns = espargos.util.estimate_toas_rootmusic(csi_backlog, per_board_average=do_average) * 1e9
        elif algorithm == "unwrap":
            phases = np.unwrap(np.angle(csi_interp), axis=-1)
            tdoas_ns = (phases[..., -1] - phases[..., 0]) / (2 * np.pi * phases.shape[-1]) / espargos.constants.WIFI_SUBCARRIER_SPACING * 1e9
            if do_average:
                tdoas_ns = np.mean(tdoas_ns, axis=(1, 2))
        else:
            sum_axis = -1 if not do_average else (1, 2, 3)
            tdoas_ns = (
                np.angle(
                    np.sum(
                        csi_interp[..., 1:] * np.conj(csi_interp[..., :-1]),
                        axis=sum_axis,
                    )
                )
                / (2 * np.pi)
                / espargos.constants.WIFI_SUBCARRIER_SPACING
                * 1e9
            )

        self.updateTDOAs.emit(mean_rx_timestamp, tdoas_ns.astype(float).flatten().tolist())


app = EspargosDemoTDOAOverTime(sys.argv)
sys.exit(app.exec())
