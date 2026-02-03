#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, ESPARGOSApplicationFlags, ConfigManager

import numpy as np
import matplotlib
import espargos
import argparse

import PyQt6.QtCore


class EspargosDemoCombinedArray(ESPARGOSApplication):
    updateColors = PyQt6.QtCore.pyqtSignal(list)
    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {}

    def __init__(self, argv):
        # Parse command line arguments
        parser = argparse.ArgumentParser(description="ESPARGOS Demo: Combined ESPARGOS arrays", add_help=False)
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(
            argv,
            argparse_parent=parser,
            flags={
                ESPARGOSApplicationFlags.ENABLE_BACKLOG,
                ESPARGOSApplicationFlags.COMBINED_ARRAY,
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

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "combined-array-ui.qml",
            {
                "appconfig": self.appconfig,
            },
        )

    def _on_update_app_state(self, newcfg):
        self.appconfig.updateAppStateHandled.emit()

    def _on_preamble_format_changed(self, newcfg):
        self.preambleFormatChanged.emit()
        self.genericconfig.updateAppStateHandled.emit()

    @PyQt6.QtCore.pyqtSlot()
    def updateRequest(self):
        if not hasattr(self, "backlog"):
            return

        csi_key = self.genericconfig.get("preamble_format")

        try:
            csi_backlog = self.backlog.get(csi_key)
        except ValueError:
            print(f"Requested CSI key {csi_key} not in backlog")
            return

        if csi_backlog.size == 0:
            return

        # Make sure backlog does not contain NaNs (happens if requesting wrong type of preamble)
        if np.any(np.isnan(csi_backlog)):
            return

        csi_largearray = espargos.util.build_combined_array_csi(self.indexing_matrix, csi_backlog)

        R = np.einsum("dnis,dmjs->nimj", csi_largearray, np.conj(csi_largearray))
        R = np.reshape(R, (R.shape[0] * R.shape[1], R.shape[2] * R.shape[3]))
        w, v = np.linalg.eig(R)
        csi_smoothed = v[:, np.argmax(w)]
        offsets_current = csi_smoothed.flatten()

        phases = np.angle(offsets_current * np.exp(-1.0j * np.angle(offsets_current[0]))).tolist()

        norm = matplotlib.colors.Normalize(vmin=-np.pi, vmax=np.pi, clip=True)
        mapper = matplotlib.cm.ScalarMappable(norm=norm, cmap="twilight")

        self.updateColors.emit(mapper.to_rgba(phases).tolist())

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def numberOfRows(self):
        return self.n_rows

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def numberOfColumns(self):
        return self.n_cols


app = EspargosDemoCombinedArray(sys.argv)
sys.exit(app.exec())
