#!/usr/bin/env python

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, SingleCSIFormatMixin

import numpy as np
import espargos
import argparse
import time

import PyQt6.QtCore


class EspargosDemoPhasesOverTime(BacklogMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    updatePhases = PyQt6.QtCore.pyqtSignal(float, list)
    maxAgeChanged = PyQt6.QtCore.pyqtSignal()
    shiftPeakChanged = PyQt6.QtCore.pyqtSignal()
    referenceChanged = PyQt6.QtCore.pyqtSignal()
    requiredAntennasChanged = PyQt6.QtCore.pyqtSignal()
    colorModeChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {"max_age": 10.0, "shift_peak": False, "reference": 0, "required_antennas": None, "color_mode": "antenna"}

    def __init__(self, argv):
        # Parse command line arguments
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Show phases over time (single board)",
            add_help=False,
        )
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(
            argv,
            argparse_parent=parser,
        )

        # Set up ESPARGOS pool and backlog
        self.initialize_pool(calibrate=not self.args.no_calib, backlog_cb_predicate=self._cluster_predicate)

        self.startTimestamp = time.time()

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "phases-over-time-ui.qml",
        )

    def _on_update_app_state(self, newcfg):
        if "max_age" in newcfg:
            self.maxAgeChanged.emit()

        if "shift_peak" in newcfg:
            self.shiftPeakChanged.emit()

        if "reference" in newcfg:
            self.referenceChanged.emit()

        if "required_antennas" in newcfg:
            self.requiredAntennasChanged.emit()

        if "color_mode" in newcfg:
            self.colorModeChanged.emit()

        super()._on_update_app_state(newcfg)

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        if self.appconfig.get("required_antennas") is None:
            self.appconfig.set({"required_antennas": int(np.prod(self.pool.get_shape()))})
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=maxAgeChanged)
    def maxCSIAge(self):
        return self.appconfig.get("max_age")

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=shiftPeakChanged)
    def shiftPeak(self):
        return self.appconfig.get("shift_peak")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=referenceChanged)
    def reference(self):
        return self.appconfig.get("reference")

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def sensorCount(self):
        return np.prod(self.pool.get_shape())

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def sensorCountPerBoard(self):
        return int(np.prod(self.pool.get_shape()[1:]))

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=requiredAntennasChanged)
    def requiredAntennas(self):
        configured = self.appconfig.get("required_antennas")
        return int(self.sensorCount if configured is None else configured)

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=colorModeChanged)
    def colorMode(self):
        return str(self.appconfig.get("color_mode"))

    def _cluster_predicate(self, cluster):
        completion = cluster.get_completion()
        return bool(np.sum(completion) >= self.requiredAntennas)

    @PyQt6.QtCore.pyqtSlot()
    def update(self):
        allow_partial = self.requiredAntennas < int(self.sensorCount)
        result = self.get_backlog_csi("host_timestamp", allow_incomplete=allow_partial)
        if result is None:
            return

        csi_backlog, timestamp_backlog = result
        timestamp = timestamp_backlog[-1] - self.startTimestamp
        valid_antennas = np.any(np.isfinite(csi_backlog), axis=(0, -1)).reshape(-1)
        if not np.any(valid_antennas):
            return

        csi_for_average = np.nan_to_num(csi_backlog, nan=0.0)
        csi_shifted = espargos.util.shift_to_firstpeak_sync(csi_for_average) if self.appconfig.get("shift_peak") else csi_for_average
        csi_interp = espargos.util.csi_interp_iterative(csi_shifted)
        csi_flat = np.reshape(csi_interp, (-1, csi_interp.shape[-1]))

        # TODO: Deal with non-synchronized multi-board setup
        csi_by_antenna = espargos.util.csi_interp_iterative(np.transpose(csi_flat))
        reference_idx = self.appconfig.get("reference")
        reference_idx = min(reference_idx, len(csi_by_antenna) - 1)  # Clamp to valid range
        if not valid_antennas[reference_idx]:
            reference_idx = int(np.flatnonzero(valid_antennas)[0])
        offsets_current_angles = np.full(len(csi_by_antenna), np.nan, dtype=np.float32)
        offsets_current_angles[valid_antennas] = np.angle(csi_by_antenna[valid_antennas] * np.exp(-1.0j * np.angle(csi_by_antenna[reference_idx])))

        self.updatePhases.emit(timestamp, offsets_current_angles.tolist())


app = EspargosDemoPhasesOverTime(sys.argv)
sys.exit(app.exec())
