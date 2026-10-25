#!/usr/bin/env python

import pathlib
import sys
import time

sys.path.append(str(pathlib.Path(__file__).absolute().parents[3]))

from demos.wifi.common.application import ESPARGOSCSIApplication, CSIBacklogMixin, SingleCSIFormatMixin
from demos.common.application import CombinedArrayMixin
from processing import music_spectrum_db, steering_vectors

import numpy as np
import espargos
import argparse

import PyQt6.QtCharts
import PyQt6.QtCore


class EspargosDemoMusicSpectrum(CSIBacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin, ESPARGOSCSIApplication):

    DEFAULT_CONFIG = {"sources": 1}

    def __init__(self, argv):
        # Parse command line arguments
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Show MUSIC angle of arrival spectrum (single or combined arrays)",
            add_help=False,
        )
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(
            argv,
            argparse_parent=parser,
        )

        # Set up ESPARGOS pool and backlog
        self.initialize_pool(calibrate=not self.args.no_calib)

        self.scanning_angles = np.deg2rad(np.linspace(-90, 90, 361))
        self._steering_frequency = None
        self._axis_updated = time.monotonic()
        self.steering_vectors = None

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "Main.qml",
        )

    def _prepare_pool_init(self, additional_calibrate_args):
        result = super()._prepare_pool_init(additional_calibrate_args)
        if not 1 <= int(self.appconfig.get("sources")) < self.n_cols:
            raise ValueError("Source count must be at least one and smaller than the number of array columns")
        return result

    @PyQt6.QtCore.pyqtSlot(PyQt6.QtCharts.QLineSeries, PyQt6.QtCharts.QValueAxis)
    def updateSpatialSpectrum(self, series, axis):
        result = self.get_backlog_csi("rx_gain", "fft_gain", remove_global_sto=False, return_format=True)
        if result is None:
            return
        preamble, csi, rx_gain, fft_gain = result
        csi = espargos.csi_processing.scale_csi_by_reported_gain(csi, rx_gain, fft_gain)
        csi = espargos.combined_array.build_combined_array_data(self.indexing_matrix, csi)

        config = self.pooldrawer.configManager()
        primary = int(config.get("channel"))
        secondary = int(config.get("secondary_channel"))
        secondary = -1 if secondary == 2 else secondary
        frequency = espargos.csi_processing.get_center_frequency(primary, primary + 4 * secondary if preamble == "ht40" else None)
        if frequency != self._steering_frequency:
            self.steering_vectors = steering_vectors(self.n_cols, self.scanning_angles, frequency)
            self._steering_frequency = frequency
        spectrum = music_spectrum_db(csi, self.steering_vectors, int(self.appconfig.get("sources")))
        if spectrum is None:
            series.clear()
            return
        data = [PyQt6.QtCore.QPointF(angle, power) for angle, power in zip(np.rad2deg(self.scanning_angles), spectrum)]
        series.replace(data)
        # Ease the visible range over three seconds, independent of refresh rate.
        now = time.monotonic()
        elapsed = min(now - self._axis_updated, 0.25)
        self._axis_updated = now
        target = min(-10.0, float(np.min(spectrum)) - 3.0)
        axis.setMin(axis.min() + (1 - np.exp(-elapsed / 3.0)) * (target - axis.min()))
        axis.setMax(2.0)  # The spectrum's peak is normalized to 0 dB.

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def maximumSources(self):
        return self.n_cols - 1

    @PyQt6.QtCore.pyqtProperty(list, constant=True)
    def scanningAngles(self):
        return np.rad2deg(self.scanning_angles).tolist()


if __name__ == "__main__":
    app = EspargosDemoMusicSpectrum(sys.argv)
    sys.exit(app.exec())
