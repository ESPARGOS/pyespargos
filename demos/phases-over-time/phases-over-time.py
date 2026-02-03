#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, ESPARGOSApplicationFlags, ConfigManager

import numpy as np
import espargos
import argparse
import time

import PyQt6.QtCore


class EspargosDemoPhasesOverTime(ESPARGOSApplication):
    updatePhases = PyQt6.QtCore.pyqtSignal(float, list)
    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()
    maxAgeChanged = PyQt6.QtCore.pyqtSignal()
    shiftPeakChanged = PyQt6.QtCore.pyqtSignal()
    referenceChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {"max_age": 10.0, "shift_peak": False, "reference": 0}

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
            flags={
                ESPARGOSApplicationFlags.ENABLE_BACKLOG,
                ESPARGOSApplicationFlags.SINGLE_PREAMBLE_FORMAT,
            },
        )

        # Set up ESPARGOS pool and backlog
        self.initialize_pool(calibrate=not self.args.no_calib)

        # App configuration manager
        self.appconfig = ConfigManager(self.DEFAULT_CONFIG, parent=self)
        self.appconfig.updateAppState.connect(self._on_update_app_state)

        # Apply optional YAML config to pool/demo config managers
        self.appconfig.set(self.get_initial_config("app", default={}))

        # Subscribe to preamble format changes
        self.genericconfig.updateAppState.connect(self._on_preamble_format_changed)

        self.startTimestamp = time.time()

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "phases-over-time-ui.qml",
            {
                "appconfig": self.appconfig,
            },
        )

    def _on_update_app_state(self, newcfg):
        if "max_age" in newcfg:
            self.maxAgeChanged.emit()

        if "shift_peak" in newcfg:
            self.shiftPeakChanged.emit()

        if "reference" in newcfg:
            self.referenceChanged.emit()

        self.appconfig.updateAppStateHandled.emit()

    def _on_preamble_format_changed(self, newcfg):
        self.preambleFormatChanged.emit()
        self.genericconfig.updateAppStateHandled.emit()

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

    @PyQt6.QtCore.pyqtSlot()
    def update(self):
        if not hasattr(self, "backlog"):
            return

        if not self.backlog.nonempty():
            return

        csi_key = self.genericconfig.get("preamble_format")

        try:
            csi_backlog = self.backlog.get(csi_key)
            timestamp = self.backlog.get_latest("host_timestamp") - self.startTimestamp
        except ValueError:
            print(f"Requested CSI key {csi_key} not in backlog")
            return

        if csi_backlog.size == 0:
            return

        # Interpolate DC subcarrier gap in HT20 / HT40 mode
        if csi_key == "ht20":
            espargos.util.interpolate_ht20ltf_gap(csi_backlog)
        elif csi_key == "ht40":
            espargos.util.interpolate_ht40ltf_gap(csi_backlog)

        csi_shifted = espargos.util.shift_to_firstpeak_sync(csi_backlog) if self.appconfig.get("shift_peak") else csi_backlog
        csi_interp = espargos.util.csi_interp_iterative(csi_shifted)
        csi_flat = np.reshape(csi_interp, (-1, csi_interp.shape[-1]))

        # TODO: Deal with non-synchronized multi-board setup
        csi_by_antenna = espargos.util.csi_interp_iterative(np.transpose(csi_flat))
        reference_idx = self.appconfig.get("reference")
        reference_idx = min(reference_idx, len(csi_by_antenna) - 1)  # Clamp to valid range
        offsets_current_angles = np.angle(csi_by_antenna * np.exp(-1.0j * np.angle(csi_by_antenna[reference_idx]))).tolist()

        self.updatePhases.emit(timestamp, offsets_current_angles)


app = EspargosDemoPhasesOverTime(sys.argv)
sys.exit(app.exec())
