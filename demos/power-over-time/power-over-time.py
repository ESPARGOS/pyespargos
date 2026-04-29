#!/usr/bin/env python

import argparse
import pathlib
import sys
import time

import numpy as np
import PyQt6.QtCore

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import espargos
import espargos.csi
from demos.common import BacklogMixin, CombinedArrayMixin, ESPARGOSApplication, SingleCSIFormatMixin


class EspargosDemoPowerOverTime(BacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    updatePowers = PyQt6.QtCore.pyqtSignal(float, list, float, float)
    maxAgeChanged = PyQt6.QtCore.pyqtSignal()
    modeChanged = PyQt6.QtCore.pyqtSignal()
    sensorModeChanged = PyQt6.QtCore.pyqtSignal()
    selectedSensorChanged = PyQt6.QtCore.pyqtSignal()
    subcarrierChanged = PyQt6.QtCore.pyqtSignal()
    scaleChanged = PyQt6.QtCore.pyqtSignal()
    compensateRssiChanged = PyQt6.QtCore.pyqtSignal()
    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "max_age": 10.0,
        "mode": "sum",  # "sum", "subcarrier"
        "sensor_mode": "all",  # "all", "single", "sum"
        "selected_sensor": 0,
        "subcarrier": 0,
        "scale": "log",  # "log", "linear"
        "compensate_rssi": True,
    }
    _Y_AXIS_RESET_KEYS = {"mode", "sensor_mode", "selected_sensor", "subcarrier", "scale", "compensate_rssi"}
    _APP_STATE_SIGNALS = {
        "max_age": "maxAgeChanged",
        "mode": "modeChanged",
        "sensor_mode": "sensorModeChanged",
        "selected_sensor": "selectedSensorChanged",
        "subcarrier": "subcarrierChanged",
        "scale": "scaleChanged",
        "compensate_rssi": "compensateRssiChanged",
    }

    def __init__(self, argv):
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Show received power over time",
            add_help=False,
        )
        parser.add_argument(
            "hosts",
            nargs="?",
            type=str,
            default="",
            help="Comma-separated list of host addresses (IP or hostname) of ESPARGOS devices",
        )
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(argv, argparse_parent=parser)

        self.initialize_pool(calibrate=not self.args.no_calib)
        self.startTimestamp = time.time()
        self._stable_y_min = None
        self._stable_y_max = None
        self.last_preamble_format = self._configured_preamble_format()
        self.preambleFormatChanged.connect(self._clamp_subcarrier_to_range)

        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "power-over-time-ui.qml")

    def _process_args(self):
        super()._process_args()
        self._use_combined_array = bool(self.args.single_array) or self.get_initial_config("combined-array") not in (None, {})

        if not self._use_combined_array and self.args.hosts:
            self.initial_config["pool"]["hosts"] = self.args.hosts.split(",")

    def _prepare_pool_init(self, additional_calibrate_args):
        if self._use_combined_array:
            return super()._prepare_pool_init(additional_calibrate_args)
        return ESPARGOSApplication._prepare_pool_init(self, additional_calibrate_args)

    def _on_update_app_state(self, newcfg):
        changed_keys = set(newcfg)
        if self._Y_AXIS_RESET_KEYS & changed_keys:
            self._stable_y_min = None
            self._stable_y_max = None

        for key in changed_keys:
            signal_name = self._APP_STATE_SIGNALS.get(key)
            if signal_name is not None:
                getattr(self, signal_name).emit()

        super()._on_update_app_state(newcfg)

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=maxAgeChanged)
    def maxCSIAge(self):
        return self.appconfig.get("max_age")

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=modeChanged)
    def mode(self):
        return self.appconfig.get("mode")

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=sensorModeChanged)
    def sensorMode(self):
        return self.appconfig.get("sensor_mode")

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=scaleChanged)
    def scale(self):
        return self.appconfig.get("scale")

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def sensorCount(self):
        if getattr(self, "_use_combined_array", False):
            return int(self.n_rows * self.n_cols)
        return int(np.prod(self.pool.get_shape()))

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=selectedSensorChanged)
    def selectedSensor(self):
        return int(self.appconfig.get("selected_sensor"))

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=preambleFormatChanged)
    def minSubcarrierIndex(self):
        return self._subcarrier_bounds[0]

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=preambleFormatChanged)
    def maxSubcarrierIndex(self):
        return self._subcarrier_bounds[1]

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=subcarrierChanged)
    def subcarrier(self):
        return int(self.appconfig.get("subcarrier"))

    @property
    def _subcarrier_bounds(self):
        preamble_format = self.genericconfig.get("preamble_format")
        if preamble_format == "auto":
            preamble_format = self.last_preamble_format
        return self._subcarrier_bounds_for_format(preamble_format)

    @staticmethod
    def _subcarrier_bounds_for_format(preamble_format):
        subcarrier_indices = espargos.csi.get_csi_format_subcarrier_indices(preamble_format)
        return int(subcarrier_indices[0]), int(subcarrier_indices[-1])

    @staticmethod
    def _interpolate_axis_max(previous, new):
        if previous is None or new >= previous:
            return new
        return previous * 0.97 + new * 0.03

    @staticmethod
    def _interpolate_axis_min(previous, new):
        if previous is None or new <= previous:
            return new
        return previous * 0.97 + new * 0.03

    def _clamp_subcarrier_to_range(self):
        clamped = int(np.clip(self.subcarrier, *self._subcarrier_bounds))
        if clamped != self.subcarrier:
            self.appconfig.set({"subcarrier": clamped})

    @PyQt6.QtCore.pyqtSlot()
    def clampSubcarrierToRange(self):
        self._clamp_subcarrier_to_range()

    def _subcarrier_array_index(self, preamble_format):
        selected = int(np.clip(self.subcarrier, *self._subcarrier_bounds_for_format(preamble_format)))
        subcarrier_indices = espargos.csi.get_csi_format_subcarrier_indices(preamble_format)
        return int(np.argmin(np.abs(subcarrier_indices - selected)))

    def _compute_power_datapoints(self, preamble_format, csi_backlog, rssi_backlog):
        csi = np.array(csi_backlog, copy=True)
        if self.appconfig.get("compensate_rssi") and self.pooldrawer.cfgman.get("gain", "automatic"):
            csi *= 10 ** (np.asarray(rssi_backlog)[..., np.newaxis] / 20)

        if getattr(self, "_use_combined_array", False):
            csi = espargos.util.build_combined_array_data(self.indexing_matrix, csi)

        if self.mode == "subcarrier":
            power = np.abs(csi[..., self._subcarrier_array_index(preamble_format)]) ** 2
        else:
            power = np.mean(np.abs(csi) ** 2, axis=-1)

        return power.reshape(power.shape[0], -1)

    @staticmethod
    def _average_valid_power(power_by_datapoint):
        valid_mask = np.isfinite(power_by_datapoint)
        valid_counts = np.sum(valid_mask, axis=0)
        safe_power = np.where(valid_mask, power_by_datapoint, 0.0)
        averaged_power = np.sum(safe_power, axis=0) / np.maximum(valid_counts, 1)
        return averaged_power, valid_counts > 0

    def _select_curves(self, power_values):
        if self.sensorMode == "sum":
            curves = np.zeros(self.sensorCount, dtype=np.float64)
            curves[0] = float(np.sum(power_values))
            visible_mask = np.zeros(self.sensorCount, dtype=bool)
            visible_mask[0] = True
            return curves, visible_mask

        visible_mask = np.ones(self.sensorCount, dtype=bool)
        curves = np.asarray(power_values, dtype=np.float64)
        if self.sensorMode == "single":
            visible_mask[:] = False
            visible_mask[int(np.clip(self.selectedSensor, 0, self.sensorCount - 1))] = True
        return curves, visible_mask

    @PyQt6.QtCore.pyqtSlot()
    def update(self):
        if (result := self.get_backlog_csi("rssi", "host_timestamp", return_format=True)) is None:
            return

        csi_key, csi_backlog, rssi_backlog, timestamp_backlog = result
        if csi_key != self.last_preamble_format:
            self.last_preamble_format = csi_key
            self.preambleFormatChanged.emit()
        power_by_datapoint = self._compute_power_datapoints(csi_key, csi_backlog, rssi_backlog)
        averaged_power, valid_sensor_mask = self._average_valid_power(power_by_datapoint)
        averaged_power = np.nan_to_num(averaged_power, nan=0.0, posinf=np.finfo(np.float64).max, neginf=0.0)
        curve_power, visible_mask = self._select_curves(averaged_power)
        visible_mask &= valid_sensor_mask

        if not np.any(visible_mask):
            return

        visible_power = np.nan_to_num(curve_power[visible_mask], nan=0.0, posinf=np.finfo(np.float64).max, neginf=0.0)

        if self.scale == "log":
            display_values = 10 * np.log10(np.maximum(curve_power, 1e-12))
            visible_display = 10 * np.log10(visible_power + 1e-12)
            raw_y_min = float(np.min(visible_display))
            raw_y_max = float(np.max(visible_display))
            raw_span = raw_y_max - raw_y_min
            padding = max(6.0, raw_span * 0.2)
            span = max(raw_span + 2 * padding, 24.0)
            center = (raw_y_min + raw_y_max) / 2.0
            y_min = center - span / 2.0
            y_max = center + span / 2.0
        else:
            display_values = np.nan_to_num(curve_power, nan=0.0, posinf=np.finfo(np.float64).max, neginf=0.0)
            y_min = 0.0
            y_max = float(np.max(visible_power) * 1.1)

        if not np.all(np.isfinite(display_values)):
            return
        if not np.isfinite(y_min) or not np.isfinite(y_max):
            return
        if y_max <= y_min:
            y_max = y_min + 1.0

        self._stable_y_min = self._interpolate_axis_min(self._stable_y_min, y_min)
        self._stable_y_max = self._interpolate_axis_max(self._stable_y_max, y_max)

        timestamp = float(timestamp_backlog[-1] - self.startTimestamp)
        self.updatePowers.emit(timestamp, display_values.astype(float).tolist(), self._stable_y_min, self._stable_y_max)


app = EspargosDemoPowerOverTime(sys.argv)
sys.exit(app.exec())
