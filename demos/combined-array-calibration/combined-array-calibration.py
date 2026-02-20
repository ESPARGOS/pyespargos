#!/usr/bin/env python

import argparse
from html import parser
import pathlib
import sys

from tomlkit import key

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))
sys.path.append(str(pathlib.Path(__file__).absolute().parents[1]))

import numpy as np
import espargos

import PyQt6.QtCharts
import PyQt6.QtCore

from common import ESPARGOSApplication, CombinedArrayMixin, SingleCSIFormatMixin, ConfigManager


class EspargosDemoCombinedArrayCalibration(CombinedArrayMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    DEFAULT_CONFIG = {
        "color_by_sensor_index": False,
        "update_rate": 0.01,
        "boardwise": False,
    }

    sensorCountChanged = PyQt6.QtCore.pyqtSignal()
    subcarrierRangeChanged = PyQt6.QtCore.pyqtSignal()
    colorBySensorIndexChanged = PyQt6.QtCore.pyqtSignal()

    def __init__(self, argv):
        parser = argparse.ArgumentParser(description="Combined Array Calibration Tool", add_help=False)
        parser.add_argument(
            "-f",
            "--outfile",
            type=str,
            default="",
            help="Path to .npy file to save calibration result upon exit",
        )

        super().__init__(
            argv,
            argparse_parent=parser,
        )

        # App-specific configuration
        self.appconfig = ConfigManager(self.DEFAULT_CONFIG, parent=self)
        self.appconfig.updateAppState.connect(self.onConfigUpdate)

        # Apply optional YAML config to app config manager
        self.appconfig.set(self.get_initial_config("app", default={}))

        # Calibration setup
        self.calibration_values = None
        self._subcarrier_range = [
            -32,
            32,
        ]  # Placeholder, will be updated on init complete
        self._sensor_count = 0
        self._sensor_count_per_board = 0

        # Initialize pool with combined array support
        self.initialize_pool(calibrate=True)
        self.initComplete.connect(self.onInitComplete)

        # Register callback for preamble format changes
        self.genericconfig.updateAppState.connect(self.onGeneralConfigUpdate)

        # Initialize QML UI
        qml_file = pathlib.Path(__file__).resolve().parent / "combined-array-calibration-ui.qml"
        self.initialize_qml(qml_file, context_props={"appconfig": self.appconfig})

    def onInitComplete(self):
        # Set up CSI callback
        self.pool.add_csi_callback(self.onCSI)

        # Set up poll timer
        self.poll_timer = PyQt6.QtCore.QTimer(self)
        self.poll_timer.timeout.connect(self.poll_csi)
        self.poll_timer.start(10)

        self.onGeneralConfigUpdate(self.genericconfig.get())

        # Calculate sensor counts
        boardwise = self.appconfig.get("boardwise")
        self._sensor_count = self.pool.get_shape()[0] if boardwise else int(np.prod(self.pool.get_shape()))
        self._sensor_count_per_board = 1 if boardwise else int(np.prod(self.pool.get_shape()[1:]))
        self.sensorCountChanged.emit()

    def exec(self):
        return super().exec()

    def onConfigUpdate(self, newcfg):
        if "boardwise" in newcfg:
            # Recalculate sensor counts
            self._sensor_count = self.pool.get_shape()[0] if newcfg["boardwise"] else int(np.prod(self.pool.get_shape()))
            self._sensor_count_per_board = 1 if newcfg["boardwise"] else int(np.prod(self.pool.get_shape()[1:]))
            self.sensorCountChanged.emit()

        if "color_by_sensor_index" in newcfg:
            self.colorBySensorIndexChanged.emit()

        self.appconfig.updateAppStateHandled.emit()

    def onGeneralConfigUpdate(self, newcfg):
        # Reset stored calibration values on preamble format change
        if "preamble_format" in newcfg:
            self.calibration_values = None

        # Calculate subcarrier range based on preamble format
        preamble_format = self.genericconfig.get("preamble_format")
        if preamble_format == "lltf":
            self.subcarrier_count = espargos.csi.LEGACY_COEFFICIENTS_PER_CHANNEL
        elif preamble_format == "ht20":
            self.subcarrier_count = espargos.csi.HT_COEFFICIENTS_PER_CHANNEL
        else:
            self.subcarrier_count = espargos.csi.HT_COEFFICIENTS_PER_CHANNEL + espargos.csi.HT40_GAP_SUBCARRIERS + espargos.csi.HT_COEFFICIENTS_PER_CHANNEL

        self._subcarrier_range = list(range(-self.subcarrier_count // 2, self.subcarrier_count // 2))
        self.subcarrierRangeChanged.emit()

    def poll_csi(self):
        self.pool.run()

    def onCSI(self, clustered_csi):
        preamble_format = self.genericconfig.get("preamble_format")

        assert self.pool.get_calibration() is not None

        # Deserialize CSI based on preamble format
        if preamble_format == "lltf":
            if not clustered_csi.has_lltf():
                print("Received CSI without LLTF data; skipping calibration update.")
                return
            csi = clustered_csi.deserialize_csi_lltf()
            csi = self.pool.get_calibration().apply_lltf(csi)
            espargos.util.remove_mean_sto(csi)
            espargos.util.interpolate_lltf_gap(csi)
        elif preamble_format == "ht20":
            if not clustered_csi.has_ht20ltf():
                print("Received CSI without HT20-LTF data; skipping calibration update.")
                return
            csi = clustered_csi.deserialize_csi_ht20ltf()
            csi = self.pool.get_calibration().apply_ht20(csi)
            espargos.util.remove_mean_sto(csi)
            espargos.util.interpolate_ht20ltf_gap(csi)
        elif preamble_format == "ht40":
            if not clustered_csi.has_ht40ltf():
                print("Received CSI without HT40-LTF data; skipping calibration update.")
                return
            csi = clustered_csi.deserialize_csi_ht40ltf()
            csi = self.pool.get_calibration().apply_ht40(csi)
            espargos.util.remove_mean_sto(csi)
            espargos.util.interpolate_ht40ltf_gap(csi)

        else:
            raise ValueError(f"Unsupported preamble format: {preamble_format}")

        # Update calibration values with exponential decay filter
        update_rate = self.appconfig.get("update_rate")
        if self.calibration_values is None:
            self.calibration_values = csi
        else:
            if csi.shape != self.calibration_values.shape:
                print("Warning: Received CSI shape does not match stored calibration values; possibly reset in progress.")
                return

            csi_to_interpolate = np.asarray([csi, self.calibration_values])
            weights = np.asarray([update_rate, 1.0 - update_rate])
            self.calibration_values = espargos.util.csi_interp_iterative(csi_to_interpolate, weights)

    @PyQt6.QtCore.pyqtSlot(list)
    def updateCalibrationResult(self, phaseSeries):
        if self.calibration_values is not None:
            csi = self.calibration_values
            boardwise = self.appconfig.get("boardwise")
            if boardwise:
                csi = np.sum(csi, axis=(1, 2))
            csi_flat = np.reshape(csi, (-1, csi.shape[-1]))
            csi_phase = np.angle(csi_flat * np.exp(-1.0j * np.angle(csi_flat[0, csi_flat.shape[1] // 2])))

            if len(phaseSeries) != len(csi_phase):
                print("Warning: Number of phase series matches number of sensors; possibly UI update in progress.")
                return

            for phase_series, ant_phase in zip(phaseSeries, csi_phase):
                phase_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(self._subcarrier_range, ant_phase)])

    def onAboutToQuit(self):
        outfile = self.args.outfile
        if len(outfile) > 0:
            np.save(outfile, self.calibration_values)
        super().onAboutToQuit()

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=sensorCountChanged)
    def sensorCount(self):
        return self._sensor_count

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=sensorCountChanged)
    def sensorCountPerBoard(self):
        return self._sensor_count_per_board

    @PyQt6.QtCore.pyqtProperty(list, constant=False, notify=subcarrierRangeChanged)
    def subcarrierRange(self):
        return self._subcarrier_range

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=colorBySensorIndexChanged)
    def colorBySensorIndex(self):
        return self.appconfig.get("color_by_sensor_index")


app = EspargosDemoCombinedArrayCalibration(sys.argv)
sys.exit(app.exec())
