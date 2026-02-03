#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, ESPARGOSApplicationFlags, ConfigManager

import numpy as np
import espargos
import argparse

import PyQt6.QtCharts
import PyQt6.QtCore

class EspargosDemoInstantaneousCSI(ESPARGOSApplication):
    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()
    displayModeChanged = PyQt6.QtCore.pyqtSignal()
    oversamplingChanged = PyQt6.QtCore.pyqtSignal()
    shiftPeakChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "display_mode": "frequency",  # "frequency", "timedomain", "music", "mvdr"
        "oversampling": 4,
        "shift_peak": False
    }

    def __init__(self, argv):
        # Parse command line arguments
        parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Show instantaneous CSI over subcarrier index (single board)", add_help = False)
        parser.add_argument("--no-calib", default = False, help = "Do not calibrate", action = "store_true")
        super().__init__(argv, argparse_parent = parser, flags = {
            ESPARGOSApplicationFlags.ENABLE_BACKLOG,
            ESPARGOSApplicationFlags.SINGLE_PREAMBLE_FORMAT
        })

        # Set up ESPARGOS pool and backlog
        self.initialize_pool(calibrate = not self.args.no_calib)

        # App configuration manager
        self.appconfig = ConfigManager(self.DEFAULT_CONFIG, parent = self)
        self.appconfig.updateAppState.connect(self._on_update_app_state)

        # Apply optional YAML config to pool/demo config managers
        self.appconfig.set(self.get_initial_config("app", default = {}))

        # Value range handling
        self.stable_power_minimum = None
        self.stable_power_maximum = None

        # Subscribe to preamble format changes
        self.genericconfig.updateAppState.connect(self._on_preamble_format_changed)
        self.sensor_count = len(self.get_initial_config("pool", "hosts")) * espargos.constants.ANTENNAS_PER_BOARD

        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "instantaneous-csi-ui.qml", {
            "appconfig": self.appconfig,
        })

    def _on_update_app_state(self, newcfg):
        # Handle display mode changes
        if "display_mode" in newcfg:
            self.stable_power_minimum = None
            self.stable_power_maximum = None
            self.displayModeChanged.emit()

        # Handle oversampling changes
        if "oversampling" in newcfg:
            self.stable_power_minimum = None
            self.stable_power_maximum = None
            self.oversamplingChanged.emit()

        # Handle shift_peak changes
        if "shift_peak" in newcfg:
            self.shiftPeakChanged.emit()

        self.appconfig.updateAppStateHandled.emit()

    def _on_preamble_format_changed(self, newcfg):
        self.preambleFormatChanged.emit()
        self.genericconfig.updateAppStateHandled.emit()

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def sensorCount(self):
        return np.prod(self.pool.get_shape())

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=displayModeChanged)
    def displayMode(self):
        return self.appconfig.get("display_mode")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=oversamplingChanged)
    def oversampling(self):
        return self.appconfig.get("oversampling")

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=shiftPeakChanged)
    def shiftPeak(self):
        return self.appconfig.get("shift_peak")

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=preambleFormatChanged)
    def preambleFormat(self):
        return self.genericconfig.get("preamble_format")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=preambleFormatChanged)
    def subcarrierCount(self):
        preamble = self.genericconfig.get("preamble_format")
        if preamble == "lltf":
            return espargos.csi.LEGACY_COEFFICIENTS_PER_CHANNEL
        elif preamble == "ht40":
            return 2 * espargos.csi.HT_COEFFICIENTS_PER_CHANNEL + espargos.csi.HT40_GAP_SUBCARRIERS
        else:
            return espargos.csi.HT_COEFFICIENTS_PER_CHANNEL

    def exec(self):
        return super().exec()

    def _interpolate_axis_range(self, previous, new):
        if previous is None:
            return new
        else:
            return (previous * 0.97 + new * 0.03)

    # list parameters contain PyQt6.QtCharts.QLineSeries
    @PyQt6.QtCore.pyqtSlot(list, list, PyQt6.QtCharts.QValueAxis, PyQt6.QtCharts.QValueAxis)
    def updateCSI(self, powerSeries, phaseSeries, subcarrierAxis, axis):
        if not hasattr(self, "backlog"):
            # Backlog not yet initialized
            return

        csi_key = self.genericconfig.get("preamble_format")

        try:
            csi_backlog, rssi_backlog = self.backlog.get_multiple([csi_key, "rssi"])
        except ValueError:
            print(f"Requested CSI key {csi_key} not in backlog")
            return

        # If any value is NaN skip this update (happens if received frame were not of expected type)
        if np.isnan(csi_backlog).any() or np.isnan(rssi_backlog).any():
            return
        
        # If backlog is empty, skip update
        if csi_backlog.size == 0:
            return

        # Weight CSI data with RSSI
        csi_backlog = csi_backlog * 10**(rssi_backlog[..., np.newaxis] / 20)

        if self.appconfig.get("shift_peak"):
            espargos.util.remove_mean_sto(csi_backlog)

        # Fill "gap" in subcarriers with interpolated data
        match csi_key:
            case "ht40":
                espargos.util.interpolate_ht40ltf_gap(csi_backlog)
            case "ht20":
                espargos.util.interpolate_ht20ltf_gap(csi_backlog)


        # TODO: If using per-board calibration, interpolation should also be per-board
        csi_interp = espargos.util.csi_interp_iterative(csi_backlog, iterations = 5)
        csi_flat = np.reshape(csi_interp, (-1, csi_interp.shape[-1]))

        display_mode = self.appconfig.get("display_mode")
        oversampling = self.appconfig.get("oversampling")

        if display_mode in ["mvdr", "music"]:
            if display_mode == "music":
                superres_delays, superres_pdps = espargos.util.fdomain_to_tdomain_pdp_music(csi_backlog)
            else:
                superres_delays, superres_pdps = espargos.util.fdomain_to_tdomain_pdp_mvdr(csi_backlog)

            superres_pdps_flat = np.reshape(superres_pdps, (-1, superres_pdps.shape[-1]))

            superres_pdps_flat = superres_pdps_flat / np.max(superres_pdps_flat)
            self.stable_power_minimum = 0
            self.stable_power_maximum = 1.1

            for pwr_series, mvdr_pdp in zip(powerSeries, superres_pdps_flat):
                pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(superres_delays, mvdr_pdp)])
        elif display_mode == "timedomain":
            csi_flat_zeropadded = np.zeros((csi_flat.shape[0], csi_flat.shape[1] * oversampling), dtype = np.complex64)
            subcarriers = csi_flat.shape[1]
            subcarriers_zp = csi_flat_zeropadded.shape[1]
            csi_flat_zeropadded[:,subcarriers_zp // 2 - subcarriers // 2:subcarriers_zp // 2 + subcarriers // 2 + 1] = csi_flat
            csi_flat_zeropadded = np.fft.ifftshift(np.fft.ifft(np.fft.fftshift(csi_flat_zeropadded, axes = -1), axis = -1), axes = -1)
            subcarrier_range_zeropadded = np.arange(-csi_flat_zeropadded.shape[-1] // 2, csi_flat_zeropadded.shape[-1] // 2) / oversampling
            csi_power = (csi_flat_zeropadded.shape[1] * np.abs(csi_flat_zeropadded))**2
            self.stable_power_minimum = 0
            self.stable_power_maximum = self._interpolate_axis_range(self.stable_power_maximum, np.max(csi_power) * 1.1)

            csi_phase = np.angle(csi_flat_zeropadded * np.exp(-1.0j * np.angle(csi_flat_zeropadded[0, len(csi_flat_zeropadded[0]) // 2])))

            for pwr_series, phase_series, ant_pwr, ant_phase in zip(powerSeries, phaseSeries, csi_power, csi_phase):
                pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range_zeropadded, ant_pwr)])
                phase_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range_zeropadded, ant_phase)])
        else:
            csi_power = 20 * np.log10(np.abs(csi_flat) + 0.00001)
            self.stable_power_minimum = self._interpolate_axis_range(self.stable_power_minimum, np.min(csi_power) - 3)
            self.stable_power_maximum = self._interpolate_axis_range(self.stable_power_maximum, np.max(csi_power) + 3)
            csi_phase = np.angle(csi_flat * np.exp(-1.0j * np.angle(csi_flat[0, csi_flat.shape[1] // 2])))
            #csi_phase = np.angle(csi_flat * np.exp(-1.0j * np.angle(csi_flat[0, :])))

            subcarrier_count = csi_flat.shape[1]
            subcarrier_range = np.arange(-subcarrier_count // 2, subcarrier_count // 2)

            for pwr_series, phase_series, ant_pwr, ant_phase in zip(powerSeries, phaseSeries, csi_power, csi_phase):
                pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range, ant_pwr)])
                phase_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range, ant_phase)])

        axis.setMin(self.stable_power_minimum)
        axis.setMax(self.stable_power_maximum)

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=displayModeChanged)
    def timeDomain(self):
        return self.appconfig.get("display_mode") == "timedomain"

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=displayModeChanged)
    def superResolution(self):
        return self.appconfig.get("display_mode") in ["mvdr", "music"]


app = EspargosDemoInstantaneousCSI(sys.argv)
sys.exit(app.exec())
