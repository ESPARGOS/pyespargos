#!/usr/bin/env python

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[3]))

from demos.wifi.common.application import ESPARGOSCSIApplication, CSIBacklogMixin, SingleCSIFormatMixin
from demos.common.application import CombinedArrayMixin

import numpy as np
from demos.common.phase_arrays import phase_arrays, sample_covariances
import espargos
import argparse

import PyQt6.QtCore


class EspargosDemoPhasesByAntenna(CSIBacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin, ESPARGOSCSIApplication):
    updatePhases = PyQt6.QtCore.pyqtSignal(list)
    backlog_cli_option = False

    DEFAULT_CONFIG = {}

    def __init__(self, argv):
        # Parse command line arguments
        parser = argparse.ArgumentParser(description="ESPARGOS Demo: Phases by Antenna (single or combined arrays)", add_help=False)
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(
            argv,
            argparse_parent=parser,
        )

        # Set up ESPARGOS pool and backlog
        self.initialize_pool(calibrate=not self.args.no_calib)

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "Main.qml",
        )

    @PyQt6.QtCore.pyqtSlot()
    def updateRequest(self):
        route = self.pooldrawer.configManager().get("rf_switch") if self.pooldrawer is not None else 2
        split = route == 4
        if (result := self.get_backlog_csi("rx_gain", "fft_gain", "rf_switch_state", allow_incomplete=True)) is None:
            self.updatePhases.emit([{}] * (2 if split else 1))
            return

        csi, rx_gain, fft_gain, feeds = (
            espargos.combined_array.build_combined_array_data(self.indexing_matrix, data)
            for data in result)
        csi = espargos.csi_processing.scale_csi_by_reported_gain(csi, rx_gain, fft_gain)
        if not split:
            csi = np.where(feeds[..., None] == route, csi, np.nan)
        covariances, counts = sample_covariances(csi.reshape(len(csi), -1, csi.shape[-1]))
        self.updatePhases.emit(phase_arrays(covariances, counts, feeds.reshape(len(csi), -1), split))

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def numberOfRows(self):
        return self.n_rows

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def numberOfColumns(self):
        return self.n_cols


if __name__ == "__main__":
    app = EspargosDemoPhasesByAntenna(sys.argv)
    sys.exit(app.exec())
