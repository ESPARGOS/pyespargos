#!/usr/bin/env python

import argparse
import pathlib
import sys

import numpy as np
import PyQt6.QtCharts
import PyQt6.QtCore

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import espargos
from demos.common import BacklogMixin, CombinedArrayMixin, ESPARGOSApplication, SingleCSIFormatMixin


class EspargosDemoStochasticFading(BacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    RAYLEIGH_SIGMA = 1.0 / np.sqrt(2.0)
    DEFAULT_X_MAX = 4.0
    AXIS_SMOOTHING = 0.15
    X_AXIS_PERCENTILE = 90.0
    X_AXIS_HEADROOM = 1.5

    binCountChanged = PyQt6.QtCore.pyqtSignal()
    maxSamplesChanged = PyQt6.QtCore.pyqtSignal()
    compensateRssiChanged = PyQt6.QtCore.pyqtSignal()
    fitModelChanged = PyQt6.QtCore.pyqtSignal()
    sampleCountChanged = PyQt6.QtCore.pyqtSignal()
    fitParameterChanged = PyQt6.QtCore.pyqtSignal()
    sampleProgressChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "bin_count": 50,
        "max_samples": 300000,
        "compensate_rssi": True,
        "fit_model": "rayleigh",
    }

    def __init__(self, argv):
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Accumulate the magnitude distribution of a single CSI coefficient",
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
        parser.add_argument(
            "--csi-completion-timeout",
            type=float,
            default=0.2,
            help="Time after which CSI cluster is considered complete even if not all antennas have provided data. Set to zero to disable processing incomplete clusters.",
        )
        super().__init__(argv, argparse_parent=parser)

        self.initialize_pool(calibrate=not self.args.no_calib, backlog_cb_predicate=self._partial_cluster_predicate)

        self._samples = np.empty(0, dtype=np.float32)
        self._last_processed_timestamp = -np.inf
        self._fit_sigma = self.RAYLEIGH_SIGMA
        self._fit_nu = 0.0
        self._normalization_scale = 1.0
        self._total_samples_seen = 0
        self._display_x_max = self.DEFAULT_X_MAX
        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "stochastic-fading-ui.qml")

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
        reset_keys = {"compensate_rssi"}
        signal_map = {
            "bin_count": self.binCountChanged,
            "max_samples": self.maxSamplesChanged,
            "compensate_rssi": self.compensateRssiChanged,
            "fit_model": self.fitModelChanged,
        }
        if "max_samples" in newcfg:
            self._trim_samples()
            self._emit_sample_state_changed()
            self._update_fit_parameters()

        for key, signal in signal_map.items():
            if key in newcfg:
                signal.emit()

        if reset_keys & set(newcfg):
            self.resetHistogram()
        elif "fit_model" in newcfg:
            self._update_fit_parameters()

        super()._on_update_app_state(newcfg)

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=binCountChanged)
    def binCount(self):
        return int(self.appconfig.get("bin_count"))

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=maxSamplesChanged)
    def maxSamples(self):
        return int(self.appconfig.get("max_samples"))

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=compensateRssiChanged)
    def compensateRSSI(self):
        return bool(self.appconfig.get("compensate_rssi"))

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=fitModelChanged)
    def fitModel(self):
        return str(self.appconfig.get("fit_model"))

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=sampleCountChanged)
    def sampleCount(self):
        return int(self._samples.size)

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=sampleProgressChanged)
    def sampleFillFraction(self):
        return min(1.0, self.sampleCount / max(1, self.maxSamples))

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=sampleProgressChanged)
    def sampleOverwritePhase(self):
        if self.maxSamples <= 0 or self._total_samples_seen < self.maxSamples:
            return self.sampleFillFraction
        return (self._total_samples_seen % self.maxSamples) / self.maxSamples

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=sampleProgressChanged)
    def sampleBufferWrapped(self):
        return self._total_samples_seen >= self.maxSamples

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=fitParameterChanged)
    def fitSigma(self):
        return float(self._fit_sigma)

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=fitParameterChanged)
    def fitNu(self):
        return float(self._fit_nu)

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=fitParameterChanged)
    def normalizationScale(self):
        return float(self._normalization_scale)

    @PyQt6.QtCore.pyqtSlot()
    def resetHistogram(self):
        self._samples = np.empty(0, dtype=np.float32)
        self._total_samples_seen = 0
        self._display_x_max = self.DEFAULT_X_MAX
        self._mark_backlog_as_seen()
        self._emit_sample_state_changed()
        self._update_fit_parameters()

    def _mark_backlog_as_seen(self):
        if not hasattr(self, "backlog") or not self.backlog.nonempty():
            self._last_processed_timestamp = -np.inf
            return

        try:
            timestamp_backlog = self.backlog.get("host_timestamp")
        except ValueError:
            self._last_processed_timestamp = -np.inf
            return

        if timestamp_backlog is None or len(timestamp_backlog) == 0:
            self._last_processed_timestamp = -np.inf
            return

        self._last_processed_timestamp = float(np.nanmax(timestamp_backlog))

    def _trim_samples(self):
        self._samples = self._samples[-max(1, self.maxSamples) :]

    def _emit_sample_state_changed(self):
        self.sampleCountChanged.emit()
        self.sampleProgressChanged.emit()

    def _partial_cluster_predicate(self, cluster):
        completion = cluster.get_completion()
        timeout_condition = False
        if self.args.csi_completion_timeout > 0:
            timeout_condition = np.sum(completion) >= 2 and cluster.get_age() > self.args.csi_completion_timeout
        return bool(np.all(completion) or timeout_condition)

    def _get_partial_backlog_csi(self, *additional_keys: str, remove_global_sto=True, return_format=False):
        if not hasattr(self, "backlog") or not self.backlog.nonempty():
            return None

        csi_key = self._resolve_backlog_preamble_format(allow_incomplete=True)

        try:
            results = list(self.backlog.get_multiple([csi_key, *additional_keys]))
        except ValueError:
            print(f"Requested CSI key {csi_key} not in backlog")
            return None

        csi_backlog = results[0]

        if remove_global_sto:
            espargos.util.remove_mean_sto(csi_backlog)

        if csi_key == "ht40":
            espargos.util.interpolate_ht40ltf_gap(csi_backlog)
        elif csi_key == "ht20":
            espargos.util.interpolate_ht20ltf_gap(csi_backlog)
        elif csi_key == "he20":
            espargos.util.interpolate_he20ltf_gaps(csi_backlog)

        if additional_keys:
            return (csi_key, *results) if return_format else tuple(results)
        return (csi_key, csi_backlog) if return_format else csi_backlog

    def _normalized_samples(self):
        return self._samples / max(self.normalizationScale, 1e-12)

    def _update_display_x_max(self, target_x_max):
        target_x_max = max(self.DEFAULT_X_MAX, float(target_x_max), 1e-3)
        self._display_x_max = (1.0 - self.AXIS_SMOOTHING) * self._display_x_max + self.AXIS_SMOOTHING * target_x_max
        self._display_x_max = max(self._display_x_max, self.DEFAULT_X_MAX)
        return self._display_x_max

    def _update_fit_parameters(self):
        sigma = self.RAYLEIGH_SIGMA
        nu = 0.0
        normalization_scale = 1.0

        if self._samples.size > 0:
            second_moment = float(np.mean(np.square(self._samples)))
            normalization_scale = float(np.sqrt(max(second_moment, 1e-12)))
            normalized_samples = self._samples / normalization_scale

            if self.fitModel == "rice" and self._samples.size >= 10:
                hist, edges = np.histogram(normalized_samples, bins=max(10, self.binCount), density=True)
                centers = 0.5 * (edges[:-1] + edges[1:])
                nu_candidates = np.linspace(0.0, 0.999, 80)

                best_error = np.inf
                for nu_candidate in nu_candidates:
                    sigma_candidate_sq = max((1.0 - nu_candidate**2) / 2.0, 1e-12)
                    sigma_candidate = np.sqrt(sigma_candidate_sq)
                    rice_pdf = self._rice_pdf(centers, nu_candidate, sigma_candidate)
                    error = float(np.mean((rice_pdf - hist) ** 2))
                    if error < best_error:
                        best_error = error
                        nu = float(nu_candidate)
                        sigma = float(sigma_candidate)
        if sigma != self._fit_sigma or nu != self._fit_nu or normalization_scale != self._normalization_scale:
            self._fit_sigma = sigma
            self._fit_nu = nu
            self._normalization_scale = normalization_scale
            self.fitParameterChanged.emit()

    @staticmethod
    def _rayleigh_pdf(x, sigma):
        sigma_sq = max(sigma**2, 1e-12)
        return x / sigma_sq * np.exp(-(x**2) / (2.0 * sigma_sq))

    @staticmethod
    def _rice_pdf(x, nu, sigma):
        sigma_sq = max(sigma**2, 1e-12)
        x = np.asarray(x, dtype=np.float64)
        z = x * nu / sigma_sq

        # Evaluate log(I0(z)) stably to avoid overflow for large z.
        log_i0 = np.empty_like(z)
        small_mask = z < 700.0
        log_i0[small_mask] = np.log(np.i0(z[small_mask]))
        large_z = z[~small_mask]
        log_i0[~small_mask] = large_z - 0.5 * np.log(2.0 * np.pi * large_z)

        pdf = np.zeros_like(x)
        positive_mask = x > 0.0
        log_pdf = np.log(x[positive_mask] / sigma_sq) - (x[positive_mask] ** 2 + nu**2) / (2.0 * sigma_sq) + log_i0[positive_mask]
        pdf[positive_mask] = np.exp(np.clip(log_pdf, -745.0, 709.0))
        return pdf

    def _append_new_samples(self):
        if (result := self._get_partial_backlog_csi("rssi", "host_timestamp", return_format=True)) is None:
            return

        _csi_key, csi_backlog, rssi_backlog, timestamp_backlog = result
        timestamp_backlog = np.asarray(timestamp_backlog, dtype=np.float64)

        new_mask = timestamp_backlog > self._last_processed_timestamp
        if not np.any(new_mask):
            return

        self._last_processed_timestamp = float(np.max(timestamp_backlog[new_mask]))

        csi_new = np.array(csi_backlog[new_mask], copy=True)
        rssi_new = np.asarray(rssi_backlog[new_mask])

        if self.compensateRSSI and self.pooldrawer.cfgman.get("gain", "automatic"):
            csi_new *= 10 ** (rssi_new[..., np.newaxis] / 20)

        if getattr(self, "_use_combined_array", False):
            csi_new = espargos.util.build_combined_array_data(self.indexing_matrix, csi_new)

        magnitudes = np.abs(csi_new).reshape(-1)
        magnitudes = magnitudes[np.isfinite(magnitudes)]

        if magnitudes.size == 0:
            return

        self._samples = np.concatenate((self._samples, magnitudes.astype(np.float32, copy=False)))
        self._total_samples_seen += int(magnitudes.size)
        self._trim_samples()
        self._emit_sample_state_changed()
        self._update_fit_parameters()

    def _fit_density(self, x):
        if self.fitModel == "rayleigh":
            return self._rayleigh_pdf(x, max(self.fitSigma, 1e-6))
        if self.fitModel == "rice":
            return self._rice_pdf(x, self.fitNu, max(self.fitSigma, 1e-6))
        return None

    @staticmethod
    def _histogram_points(edges, hist):
        points = [PyQt6.QtCore.QPointF(edges[0], 0.0)]
        for left, right, density in zip(edges[:-1], edges[1:], hist):
            points.append(PyQt6.QtCore.QPointF(left, density))
            points.append(PyQt6.QtCore.QPointF(right, density))
        points.append(PyQt6.QtCore.QPointF(edges[-1], 0.0))
        return points

    @staticmethod
    def _replace_series(series, x, y):
        series.replace([PyQt6.QtCore.QPointF(px, py) for px, py in zip(x, y)])

    @staticmethod
    def _set_axes(magnitudeAxis, densityAxis, x_max, density_max):
        magnitudeAxis.setMin(0.0)
        magnitudeAxis.setMax(x_max)
        densityAxis.setMin(0.0)
        densityAxis.setMax(density_max * 1.1)
        magnitudeAxis.setTickType(PyQt6.QtCharts.QValueAxis.TickType.TicksDynamic)
        densityAxis.setTickType(PyQt6.QtCharts.QValueAxis.TickType.TicksDynamic)
        magnitudeAxis.setTickInterval(x_max / 8.0)
        densityAxis.setTickInterval(max(density_max / 6.0, 1e-3))

    @PyQt6.QtCore.pyqtSlot(
        PyQt6.QtCharts.QLineSeries,
        PyQt6.QtCharts.QLineSeries,
        PyQt6.QtCharts.QLineSeries,
        PyQt6.QtCharts.QValueAxis,
        PyQt6.QtCharts.QValueAxis,
    )
    def updateDistribution(self, histogramUpperSeries, histogramLowerSeries, fitSeries, magnitudeAxis, densityAxis):
        self._append_new_samples()

        bin_count = max(5, self.binCount)
        target_x_max = self.DEFAULT_X_MAX
        hist = np.array([0.0])
        if self._samples.size >= 2:
            normalized_samples = self._normalized_samples()
            target_x_max = float(np.percentile(normalized_samples, self.X_AXIS_PERCENTILE)) * self.X_AXIS_HEADROOM

        x_max = self._update_display_x_max(target_x_max)
        edges = np.array([0.0, x_max])
        if self._samples.size >= 2:
            hist, edges = np.histogram(normalized_samples, bins=bin_count, range=(0.0, x_max), density=True)

        fit_x = np.linspace(0.0, x_max, max(200, 8 * bin_count))
        histogramUpperSeries.replace(self._histogram_points(edges, hist))
        histogramLowerSeries.replace([PyQt6.QtCore.QPointF(edges[0], 0.0), PyQt6.QtCore.QPointF(edges[-1], 0.0)])

        fit_density = self._fit_density(fit_x)
        if fit_density is None:
            fitSeries.replace([PyQt6.QtCore.QPointF(0.0, 0.0), PyQt6.QtCore.QPointF(x_max, 0.0)])
            density_max = float(np.max(hist))
        else:
            self._replace_series(fitSeries, fit_x, fit_density)
            density_max = max(float(np.max(hist)), float(np.max(fit_density)))

        self._set_axes(magnitudeAxis, densityAxis, x_max, density_max if density_max > 0 else 1.0)


app = EspargosDemoStochasticFading(sys.argv)
sys.exit(app.exec())
