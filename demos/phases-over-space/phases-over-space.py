#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, SingleCSIFormatMixin

import numpy as np
import matplotlib
import espargos
import argparse

import PyQt6.QtCore


class EspargosDemoPhasesOverSpace(BacklogMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    updateColors = PyQt6.QtCore.pyqtSignal(list)

    DEFAULT_CONFIG = {}

    def __init__(self, argv):
        # Parse command line arguments
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Show phases over space (single board)",
            add_help=False,
        )
        parser.add_argument(
            "--no-calib",
            default=False,
            help="Disable phase calibration",
            action="store_true",
        )
        super().__init__(
            argv,
            argparse_parent=parser,
        )

        # Set up ESPARGOS pool and backlog
        self.initialize_pool(calibrate=not self.args.no_calib)

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "phases-over-space-ui.qml",
        )

    @PyQt6.QtCore.pyqtSlot()
    def updateRequest(self):
        if (csi_backlog := self.get_backlog_csi()) is None:
            return

        R = np.einsum("dbmis,dbnjs->minj", csi_backlog, np.conj(csi_backlog))
        R = np.reshape(
            R,
            (
                espargos.constants.ANTENNAS_PER_BOARD,
                espargos.constants.ANTENNAS_PER_BOARD,
            ),
        )
        w, v = np.linalg.eig(R)
        csi_smoothed = v[:, np.argmax(w)]
        offsets_current = csi_smoothed.flatten()
        phases = np.angle(offsets_current * np.exp(-1.0j * np.angle(offsets_current[0]))).tolist()

        norm = matplotlib.colors.Normalize(vmin=-np.pi, vmax=np.pi, clip=True)
        mapper = matplotlib.cm.ScalarMappable(norm=norm, cmap="twilight")

        self.updateColors.emit(mapper.to_rgba(phases).tolist())


app = EspargosDemoPhasesOverSpace(sys.argv)
sys.exit(app.exec())
