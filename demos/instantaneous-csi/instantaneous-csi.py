#!/usr/bin/env python

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, SingleCSIFormatMixin

from espargos.csi import rfswitch_state_t
import numpy as np
import espargos
import argparse

import PyQt6.QtCharts
import PyQt6.QtCore


class EspargosDemoInstantaneousCSI(BacklogMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    # Re-declare base class signal so it's visible to notify= in pyqtProperty decorators
    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()

    displayModeChanged = PyQt6.QtCore.pyqtSignal()
    oversamplingChanged = PyQt6.QtCore.pyqtSignal()
    requiredAntennasChanged = PyQt6.QtCore.pyqtSignal()
    relativePhaseChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "display_mode": "frequency",  # "frequency", "timedomain", "constellation", "music", "mvdr"
        "oversampling": 4,
        "feed_filter": "all",
        "required_antennas": None,
        "relative_phase": True,
    }

    def __init__(self, argv):
        # Parse command line arguments
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Show instantaneous CSI over subcarrier index (single board)",
            add_help=False,
        )
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(
            argv,
            argparse_parent=parser,
        )

        # Set up ESPARGOS pool and backlog
        self.initialize_pool(calibrate=not self.args.no_calib, backlog_cb_predicate=self._cluster_predicate)

        # Value range handling
        self.stable_power_minimum = None
        self.stable_power_maximum = None
        self.last_preamble_format = self._configured_preamble_format()

        self.sensor_count = len(self.get_initial_config("pool", "hosts")) * espargos.constants.ANTENNAS_PER_BOARD

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "instantaneous-csi-ui.qml",
        )

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

        if "required_antennas" in newcfg:
            self.requiredAntennasChanged.emit()

        if "relative_phase" in newcfg:
            self.relativePhaseChanged.emit()

        super()._on_update_app_state(newcfg)

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        if self.appconfig.get("required_antennas") is None:
            self.appconfig.set({"required_antennas": int(np.prod(self.pool.get_shape()))})
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def sensorCount(self):
        return np.prod(self.pool.get_shape())

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=requiredAntennasChanged)
    def requiredAntennas(self):
        configured = self.appconfig.get("required_antennas")
        return int(self.sensorCount if configured is None else configured)

    def _cluster_predicate(self, cluster):
        completion = cluster.get_completion()
        return bool(np.sum(completion) >= self.requiredAntennas)

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=displayModeChanged)
    def displayMode(self):
        return self.appconfig.get("display_mode")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=oversamplingChanged)
    def oversampling(self):
        return self.appconfig.get("oversampling")

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=relativePhaseChanged)
    def relativePhase(self):
        return bool(self.appconfig.get("relative_phase"))

    # Mapping from config string to rfswitch_state_t
    FEED_FILTER_MAP = {
        "R": rfswitch_state_t.SENSOR_RFSWITCH_ANTENNA_R,
        "L": rfswitch_state_t.SENSOR_RFSWITCH_ANTENNA_L,
        "ref": rfswitch_state_t.SENSOR_RFSWITCH_REFERENCE,
        "iso": rfswitch_state_t.SENSOR_RFSWITCH_ISOLATION,
    }

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=preambleFormatChanged)
    def subcarrierCount(self):
        preamble_format = self.genericconfig.get("preamble_format")
        if preamble_format == "auto":
            preamble_format = self.last_preamble_format
        return espargos.csi.get_csi_format_subcarrier_count(preamble_format)

    def exec(self):
        return super().exec()

    def _interpolate_axis_range(self, previous, new):
        if previous is None:
            return new
        else:
            return previous * 0.97 + new * 0.03

    def _constellation_axis_limit(self, csi_key: str, csi_backlog: np.ndarray, lltf_8bit_mode_backlog: np.ndarray) -> int:
        if csi_key != "lltf":
            return 128

        valid_lltf_sensors = np.any(np.isfinite(csi_backlog), axis=-1)
        lltf_8bit_mode = lltf_8bit_mode_backlog[valid_lltf_sensors]
        if lltf_8bit_mode.size == 0:
            return 128
        if np.count_nonzero(lltf_8bit_mode) < np.count_nonzero(~lltf_8bit_mode):
            return 2048
        return 128

    # list parameters contain PyQt6.QtCharts.QLineSeries / QScatterSeries
    @PyQt6.QtCore.pyqtSlot(list, list, PyQt6.QtCharts.QValueAxis, PyQt6.QtCharts.QValueAxis)
    def updateCSI(self, powerSeries, phaseSeries, subcarrierAxis, axis):
        if (result := self.get_backlog_csi("rx_gain", "fft_gain", "rfswitch_state", "lltf_8bit_mode", allow_incomplete=True, return_format=True)) is None:
            return

        csi_key, csi_backlog, rx_gain_backlog, fft_gain_backlog, rfswitch_state, lltf_8bit_mode_backlog = result
        if csi_key != self.last_preamble_format:
            self.last_preamble_format = csi_key
            self.preambleFormatChanged.emit()

        valid_samples = np.any(np.isfinite(csi_backlog), axis=-1) & np.isfinite(rx_gain_backlog) & np.isfinite(fft_gain_backlog)
        if not np.any(valid_samples):
            return

        display_mode = self.appconfig.get("display_mode")
        relative_phase = self.appconfig.get("relative_phase")

        # Apply feed filter if not "all"
        feed_filter = self.appconfig.get("feed_filter")
        if feed_filter != "all" and feed_filter in self.FEED_FILTER_MAP:
            target_state = self.FEED_FILTER_MAP[feed_filter]
            valid_samples &= rfswitch_state == target_state

        valid_antennas = np.any(valid_samples, axis=0).reshape(-1)
        if not np.any(valid_antennas):
            for pwr_series, phase_series in zip(powerSeries, phaseSeries):
                pwr_series.replace([])
                phase_series.replace([])
            return

        # Keep missing antennas/samples out of the coherent average while preserving
        # the original antenna layout so existing chart series keep their identity.
        csi_backlog = np.where(valid_samples[..., np.newaxis], csi_backlog, np.nan + 1.0j * np.nan)

        if display_mode == "constellation":
            axis_limit = self._constellation_axis_limit(csi_key, csi_backlog, lltf_8bit_mode_backlog)
            subcarrierAxis.setMin(-axis_limit)
            subcarrierAxis.setMax(axis_limit - 1)
            axis.setMin(-axis_limit)
            axis.setMax(axis_limit - 1)

            if relative_phase:
                csi_backlog_flat = np.reshape(csi_backlog, (csi_backlog.shape[0], -1, csi_backlog.shape[-1]))
                reference_coefficients = csi_backlog_flat[:, 0, csi_backlog_flat.shape[-1] // 2]
                valid_reference_coefficients = np.isfinite(reference_coefficients) & (np.abs(reference_coefficients) > 0)
                phase_correction = np.ones(csi_backlog.shape[0], dtype=csi_backlog.dtype)
                phase_correction[valid_reference_coefficients] = np.exp(-1.0j * np.angle(reference_coefficients[valid_reference_coefficients]))
                csi_backlog = csi_backlog * phase_correction.reshape((-1,) + (1,) * (csi_backlog.ndim - 1))

            finite_samples = np.isfinite(csi_backlog)
            csi_sample_count = np.sum(finite_samples, axis=0)
            csi_constellation = np.divide(
                np.nansum(csi_backlog, axis=0),
                csi_sample_count,
                out=np.full(csi_backlog.shape[1:], np.nan + 1.0j * np.nan, dtype=csi_backlog.dtype),
                where=csi_sample_count > 0,
            )
            csi_flat = np.reshape(csi_constellation, (-1, csi_constellation.shape[-1]))
            for series, ant_csi in zip(powerSeries, csi_flat):
                series.replace([PyQt6.QtCore.QPointF(float(np.real(v)), float(np.imag(v))) for v in ant_csi if np.isfinite(v)])
            return

        filtered_datapoint_count = np.sum(valid_samples, axis=0)
        scaling = np.divide(
            csi_backlog.shape[0],
            filtered_datapoint_count,
            out=np.zeros_like(filtered_datapoint_count, dtype=np.float32),
            where=filtered_datapoint_count > 0,
        )
        csi_backlog *= scaling[..., np.newaxis]

        csi_backlog = np.nan_to_num(csi_backlog, nan=0.0)
        rx_gain_backlog = np.nan_to_num(rx_gain_backlog, nan=0.0)
        fft_gain_backlog = np.nan_to_num(fft_gain_backlog, nan=0.0)
        csi_backlog = espargos.util.scale_csi_by_reported_gain(csi_backlog, rx_gain_backlog, fft_gain_backlog)

        if self.pooldrawer.cfgman.get("calibration", "per_board"):
            csi_interp = espargos.util.csi_interp_iterative_by_array(csi_backlog, iterations=5)
        else:
            csi_interp = espargos.util.csi_interp_iterative(csi_backlog, iterations=5)
        csi_flat = np.reshape(csi_interp, (-1, csi_interp.shape[-1]))

        oversampling = self.appconfig.get("oversampling")

        if display_mode in ["mvdr", "music"]:
            if display_mode == "music":
                superres_delays, superres_pdps = espargos.util.fdomain_to_tdomain_pdp_music(csi_backlog)
            else:
                superres_delays, superres_pdps = espargos.util.fdomain_to_tdomain_pdp_mvdr(csi_backlog)

            superres_pdps_flat = np.reshape(superres_pdps, (-1, superres_pdps.shape[-1]))
            superres_pdps_flat_active = superres_pdps_flat[valid_antennas]

            power_max = np.max(superres_pdps_flat_active)
            if power_max <= 0:
                return
            superres_pdps_flat = superres_pdps_flat / power_max
            self.stable_power_minimum = 0
            self.stable_power_maximum = 1.1

            for is_valid, pwr_series, phase_series, mvdr_pdp in zip(valid_antennas, powerSeries, phaseSeries, superres_pdps_flat):
                if is_valid:
                    pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(superres_delays, mvdr_pdp)])
                else:
                    pwr_series.replace([])
                phase_series.replace([])
        elif display_mode == "timedomain":
            csi_flat_zeropadded = np.zeros(
                (csi_flat.shape[0], csi_flat.shape[1] * oversampling),
                dtype=np.complex64,
            )
            subcarriers = csi_flat.shape[1]
            subcarriers_zp = csi_flat_zeropadded.shape[1]
            csi_flat_zeropadded[
                :,
                subcarriers_zp // 2 - subcarriers // 2 : subcarriers_zp // 2 + subcarriers // 2 + 1,
            ] = csi_flat
            csi_flat_zeropadded = np.fft.fftshift(
                np.fft.ifft(np.fft.ifftshift(csi_flat_zeropadded, axes=-1), axis=-1),
                axes=-1,
            )
            subcarrier_range_zeropadded = (np.arange(csi_flat_zeropadded.shape[-1]) - csi_flat_zeropadded.shape[-1] // 2) / oversampling
            csi_power = csi_flat_zeropadded.shape[1] * np.abs(csi_flat_zeropadded) ** 2
            csi_power_active = csi_power[valid_antennas]
            self.stable_power_minimum = 0
            self.stable_power_maximum = self._interpolate_axis_range(self.stable_power_maximum, np.max(csi_power_active) * 1.1)

            if relative_phase:
                reference_idx = int(np.flatnonzero(valid_antennas)[0])
                csi_phase_reference = csi_flat_zeropadded[reference_idx, len(csi_flat_zeropadded[reference_idx]) // 2]
                csi_phase = np.angle(csi_flat_zeropadded * np.exp(-1.0j * np.angle(csi_phase_reference)))
            else:
                csi_phase = np.angle(csi_flat_zeropadded)

            for is_valid, pwr_series, phase_series, ant_pwr, ant_phase in zip(valid_antennas, powerSeries, phaseSeries, csi_power, csi_phase):
                if is_valid:
                    pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range_zeropadded, ant_pwr)])
                    phase_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range_zeropadded, ant_phase)])
                else:
                    pwr_series.replace([])
                    phase_series.replace([])
        else:
            csi_power = 20 * np.log10(np.abs(csi_flat) + 0.00001)
            csi_power_active = csi_power[valid_antennas]
            self.stable_power_minimum = self._interpolate_axis_range(self.stable_power_minimum, np.min(csi_power_active) - 3)
            self.stable_power_maximum = self._interpolate_axis_range(self.stable_power_maximum, np.max(csi_power_active) + 3)
            if relative_phase:
                reference_idx = int(np.flatnonzero(valid_antennas)[0])
                csi_phase = np.angle(csi_flat * np.exp(-1.0j * np.angle(csi_flat[reference_idx, csi_flat.shape[1] // 2])))
            else:
                csi_phase = np.angle(csi_flat)

            subcarrier_range = espargos.csi.get_csi_format_subcarrier_indices(csi_key)

            for is_valid, pwr_series, phase_series, ant_pwr, ant_phase in zip(valid_antennas, powerSeries, phaseSeries, csi_power, csi_phase):
                if is_valid:
                    pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range, ant_pwr)])
                    phase_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range, ant_phase)])
                else:
                    pwr_series.replace([])
                    phase_series.replace([])

        axis.setMin(self.stable_power_minimum)
        axis.setMax(self.stable_power_maximum)

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=displayModeChanged)
    def timeDomain(self):
        return self.appconfig.get("display_mode") == "timedomain"

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=displayModeChanged)
    def superResolution(self):
        return self.appconfig.get("display_mode") in ["mvdr", "music"]

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=displayModeChanged)
    def constellation(self):
        return self.appconfig.get("display_mode") == "constellation"


app = EspargosDemoInstantaneousCSI(sys.argv)
sys.exit(app.exec())
