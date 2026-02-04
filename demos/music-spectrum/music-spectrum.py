#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, SingleCSIFormatMixin, ConfigManager

import numpy as np
import espargos
import argparse

import PyQt6.QtCharts
import PyQt6.QtCore


class EspargosDemoMusicSpectrum(BacklogMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()
    shiftPeakChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {"shift_peak": False}

    def __init__(self, argv):
        # Parse command line arguments
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Show MUSIC angle of arrival spectrum (single board)",
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

        # Subscribe to preamble format changes
        self.genericconfig.updateAppState.connect(self._on_preamble_format_changed)

        # Initialize MUSIC scanning angles, steering vectors, ...
        self.scanning_angles = np.linspace(-np.pi / 2, np.pi / 2, 180)
        self.steering_vectors = np.exp(
            -1.0j
            * np.outer(
                np.pi * np.sin(self.scanning_angles),
                np.arange(espargos.constants.ANTENNAS_PER_ROW),
            )
        )
        self.spatial_spectrum = None

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "music-spectrum-ui.qml",
            {
                "appconfig": self.appconfig,
            },
        )

    def _on_update_app_state(self, newcfg):
        # Handle shift_peak changes
        if "shift_peak" in newcfg:
            self.shiftPeakChanged.emit()

        self.appconfig.updateAppStateHandled.emit()

    def _on_preamble_format_changed(self, newcfg):
        self.preambleFormatChanged.emit()
        self.genericconfig.updateAppStateHandled.emit()

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=shiftPeakChanged)
    def shiftPeak(self):
        return self.appconfig.get("shift_peak")

    @PyQt6.QtCore.pyqtSlot(PyQt6.QtCharts.QLineSeries, PyQt6.QtCharts.QValueAxis)
    def updateSpatialSpectrum(self, series, axis):
        if not hasattr(self, "backlog"):
            return

        csi_key = self.genericconfig.get("preamble_format")

        try:
            csi_backlog, rssi_backlog = self.backlog.get_multiple([csi_key, "rssi"])
        except ValueError:
            print(f"Requested CSI key {csi_key} not in backlog")
            return

        if csi_backlog.size == 0:
            return

        # If any CSI values are NaN, skip processing
        if np.any(np.isnan(csi_backlog)):
            return

        # Interpolate missing DC gap subcarriers if needed
        if csi_key == "ht20":
            espargos.util.interpolate_ht20ltf_gap(csi_backlog)
        elif csi_key == "ht40":
            espargos.util.interpolate_ht40ltf_gap(csi_backlog)

        # Weight CSI data with RSSI
        csi_backlog = csi_backlog * 10 ** (rssi_backlog[..., np.newaxis] / 20)

        # Shift to first peak if requested
        csi_shifted = espargos.util.shift_to_firstpeak_sync(csi_backlog, peak_threshold=0.5) if self.appconfig.get("shift_peak") else csi_backlog

        # Compute array covariance matrix R over all backlog datapoints, all rows and all subcarriers
        csi_shifted_los = np.sum(csi_shifted, axis=-1)
        R = np.einsum("dbri,dbrj->ij", csi_shifted_los, np.conj(csi_shifted_los))
        eig_val, eig_vec = np.linalg.eig(R)
        order = np.argsort(eig_val)[::-1]

        # TODO: Automatic / manual estimation of number of decorrelated signals (i.e., reflections with sufficient doppler)
        Qn = eig_vec[:, order][:, 1:]
        spatial_spectrum_linear = 1 / np.linalg.norm(np.einsum("ae,ra->er", np.conj(Qn), self.steering_vectors), axis=0)
        spatial_spectrum_log = 20 * np.log10(spatial_spectrum_linear)

        axis.setMin(np.min(spatial_spectrum_log) - 1)
        axis.setMax(max(np.max(spatial_spectrum_log), axis.max()))

        data = [PyQt6.QtCore.QPointF(np.rad2deg(angle), power) for angle, power in zip(self.scanning_angles, spatial_spectrum_log)]
        series.replace(data)

    @PyQt6.QtCore.pyqtProperty(list, constant=True)
    def scanningAngles(self):
        return self.scanning_angles.tolist()


app = EspargosDemoMusicSpectrum(sys.argv)
sys.exit(app.exec())
