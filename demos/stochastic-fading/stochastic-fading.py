#!/usr/bin/env python

import argparse
import pathlib
import sys

import numpy as np
import PyQt6.QtCharts
import PyQt6.QtCore

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import espargos
import espargos.csi
from demos.common import BacklogMixin, ESPARGOSApplication, SingleCSIFormatMixin


class EspargosDemoRayleighFading(BacklogMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()
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
        self._fit_sigma = 1.0 / np.sqrt(2.0)
        self._fit_nu = 0.0
        self._normalization_scale = 1.0
        self._total_samples_seen = 0
        self.preambleFormatChanged.connect(self.resetHistogram)

        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "stochastic-fading-ui.qml")

    def _on_update_app_state(self, newcfg):
        should_reset = False

        if "bin_count" in newcfg:
            self.binCountChanged.emit()

        if "max_samples" in newcfg:
            self._trim_samples()
            self.maxSamplesChanged.emit()
            self.sampleCountChanged.emit()
            self._update_fit_parameters()

        if "compensate_rssi" in newcfg:
            self.compensateRssiChanged.emit()
            should_reset = True

        if "fit_model" in newcfg:
            self.fitModelChanged.emit()
            self._update_fit_parameters()

        if should_reset:
            self.resetHistogram()

        super()._on_update_app_state(newcfg)

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=preambleFormatChanged)
    def preambleFormat(self):
        return self.genericconfig.get("preamble_format")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=preambleFormatChanged)
    def subcarrierCount(self):
        preamble = self.genericconfig.get("preamble_format")
        if preamble == "lltf":
            return espargos.csi.LEGACY_COEFFICIENTS_PER_CHANNEL
        if preamble == "ht40":
            return 2 * espargos.csi.HT_COEFFICIENTS_PER_CHANNEL + espargos.csi.HT40_GAP_SUBCARRIERS
        return espargos.csi.HT_COEFFICIENTS_PER_CHANNEL

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
        self._mark_backlog_as_seen()
        self.sampleCountChanged.emit()
        self.sampleProgressChanged.emit()
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
        max_samples = max(1, self.maxSamples)
        if self._samples.size > max_samples:
            self._samples = self._samples[-max_samples:]

    def _partial_cluster_predicate(self, completion, age):
        timeout_condition = False
        if self.args.csi_completion_timeout > 0:
            timeout_condition = np.sum(completion) >= 1 and age > self.args.csi_completion_timeout
        return bool(np.all(completion) or timeout_condition)

    def _get_partial_backlog_csi(self, *additional_keys: str, remove_global_sto=True):
        if not hasattr(self, "backlog") or not self.backlog.nonempty():
            return None

        csi_key = self.genericconfig.get("preamble_format")

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

        if additional_keys:
            return tuple(results)
        return csi_backlog

    def _update_fit_parameters(self):
        sigma = 1.0 / np.sqrt(2.0)
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
        if (result := self._get_partial_backlog_csi("rssi", "host_timestamp")) is None:
            return

        csi_backlog, rssi_backlog, timestamp_backlog = result
        timestamp_backlog = np.asarray(timestamp_backlog, dtype=np.float64)

        new_mask = timestamp_backlog > self._last_processed_timestamp
        if not np.any(new_mask):
            return

        self._last_processed_timestamp = float(np.max(timestamp_backlog[new_mask]))

        csi_new = np.array(csi_backlog[new_mask], copy=True)
        rssi_new = np.asarray(rssi_backlog[new_mask])

        if self.compensateRSSI and self.pooldrawer.cfgman.get("gain", "automatic"):
            csi_new *= 10 ** (rssi_new[..., np.newaxis] / 20)

        magnitudes = np.abs(csi_new).reshape(-1)
        magnitudes = magnitudes[np.isfinite(magnitudes)]

        if magnitudes.size == 0:
            return

        self._samples = np.concatenate((self._samples, magnitudes.astype(np.float32, copy=False)))
        self._total_samples_seen += int(magnitudes.size)
        self._trim_samples()
        self.sampleCountChanged.emit()
        self.sampleProgressChanged.emit()
        self._update_fit_parameters()

    @PyQt6.QtCore.pyqtSlot(
        PyQt6.QtCharts.QLineSeries,
        PyQt6.QtCharts.QLineSeries,
        PyQt6.QtCharts.QLineSeries,
        PyQt6.QtCharts.QValueAxis,
        PyQt6.QtCharts.QValueAxis,
    )
    def updateDistribution(self, histogramUpperSeries, histogramLowerSeries, fitSeries, magnitudeAxis, densityAxis):
        self._append_new_samples()

        if self._samples.size < 2:
            x_max = 4.0
            fit_x = np.linspace(0.0, x_max, 200)
            empty_histogram = [
                PyQt6.QtCore.QPointF(0.0, 0.0),
                PyQt6.QtCore.QPointF(x_max, 0.0),
            ]

            histogramUpperSeries.replace(empty_histogram)
            histogramLowerSeries.replace(empty_histogram)

            if self.fitModel == "rayleigh":
                fit_density = self._rayleigh_pdf(fit_x, 1.0 / np.sqrt(2.0))
                fitSeries.replace([PyQt6.QtCore.QPointF(x, y) for x, y in zip(fit_x, fit_density)])
                density_max = float(np.max(fit_density))
            elif self.fitModel == "rice":
                fit_density = self._rice_pdf(fit_x, 0.0, 1.0 / np.sqrt(2.0))
                fitSeries.replace([PyQt6.QtCore.QPointF(x, y) for x, y in zip(fit_x, fit_density)])
                density_max = float(np.max(fit_density))
            else:
                fitSeries.replace(empty_histogram)
                density_max = 1.0

            magnitudeAxis.setMin(0.0)
            magnitudeAxis.setMax(x_max)
            densityAxis.setMin(0.0)
            densityAxis.setMax(density_max * 1.1)
            magnitudeAxis.setTickType(PyQt6.QtCharts.QValueAxis.TickType.TicksDynamic)
            densityAxis.setTickType(PyQt6.QtCharts.QValueAxis.TickType.TicksDynamic)
            magnitudeAxis.setTickInterval(x_max / 8.0)
            densityAxis.setTickInterval(max(density_max / 6.0, 1e-3))
            return

        normalized_samples = self._samples / max(self.normalizationScale, 1e-12)
        sigma = max(self.fitSigma, 1e-6)
        max_sample = float(np.max(normalized_samples))
        x_max = max(max_sample * 1.05, 4.0)
        x_max = max(x_max, 1e-3)

        bin_count = max(5, self.binCount)
        hist, edges = np.histogram(normalized_samples, bins=bin_count, range=(0.0, x_max), density=True)
        fit_x = np.linspace(0.0, x_max, max(200, 8 * bin_count))

        histogram_points = [PyQt6.QtCore.QPointF(edges[0], 0.0)]
        for left, right, density in zip(edges[:-1], edges[1:], hist):
            histogram_points.append(PyQt6.QtCore.QPointF(left, density))
            histogram_points.append(PyQt6.QtCore.QPointF(right, density))
        histogram_points.append(PyQt6.QtCore.QPointF(edges[-1], 0.0))

        histogramUpperSeries.replace(histogram_points)
        histogramLowerSeries.replace([PyQt6.QtCore.QPointF(edges[0], 0.0), PyQt6.QtCore.QPointF(edges[-1], 0.0)])

        fit_density = None
        if self.fitModel == "rayleigh":
            fit_density = self._rayleigh_pdf(fit_x, sigma)
        elif self.fitModel == "rice":
            fit_density = self._rice_pdf(fit_x, self.fitNu, sigma)

        if fit_density is None:
            fitSeries.clear()
            density_max = float(np.max(hist))
        else:
            fitSeries.replace([PyQt6.QtCore.QPointF(x, y) for x, y in zip(fit_x, fit_density)])
            density_max = max(float(np.max(hist)), float(np.max(fit_density)))

        magnitudeAxis.setMin(0.0)
        magnitudeAxis.setMax(x_max)
        densityAxis.setMin(0.0)
        densityAxis.setMax(density_max * 1.1)
        magnitudeAxis.setTickType(PyQt6.QtCharts.QValueAxis.TickType.TicksDynamic)
        densityAxis.setTickType(PyQt6.QtCharts.QValueAxis.TickType.TicksDynamic)
        magnitudeAxis.setTickInterval(x_max / 8.0)
        densityAxis.setTickInterval(max(density_max / 6.0, 1e-3))


app = EspargosDemoRayleighFading(sys.argv)
sys.exit(app.exec())
