#!/usr/bin/env python
"""Relative BLE channel amplitude and phase across its bandwidth."""

from pathlib import Path
import sys
import time
import numpy as np
from PyQt6 import QtCore, QtCharts

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from demos.ble.common.application import ESPARGOSBluetoothApplication


class BLEChannelApplication(ESPARGOSBluetoothApplication):
    DEFAULT_CONFIG = {
        **ESPARGOSBluetoothApplication.DEFAULT_CONFIG,
        "correct_timing": True,
        "nfft": 128,
    }
    QML_FILE = Path(__file__).with_name("Main.qml")

    def _process_args(self):
        super()._process_args()
        minimum = self.get_initial_config("app", "min_antennas")
        if type(minimum) is not int or not 2 <= minimum <= 8:
            raise ValueError("app.min_antennas must be an integer from 2 to 8")

    def process_cluster(self, cluster):
        started = time.monotonic()
        response = self.controller.receiver.channel_response(cluster, calibrated=self.appconfig.get("calibrated"), correct_timing=self.appconfig.get("correct_timing"), nfft=int(self.appconfig.get("nfft")))

        valid = self._valid_bins(response)[0].reshape(8, -1)
        h = response.response[0].reshape(valid.shape)
        previous, last_seen = getattr(self, "_plot_reference", (None, -np.inf))
        if last_seen < self._after:
            previous = None
        # The packet alignment reference can change every packet. Express the
        # displayed ratios against one antenna while it still has shared bins.
        candidates = np.flatnonzero(valid.sum(axis=1) >= 2)
        if previous is not None:
            candidates = [previous, *(i for i in candidates if i != previous)]
        minimum = self.appconfig.get("min_antennas")
        for index in candidates:
            shared = valid & valid[index]
            if np.count_nonzero(shared.sum(axis=1) >= 2) >= minimum:
                ratios = np.full_like(h, np.nan)
                np.divide(h, h[index], out=ratios, where=shared)
                self._plot_reference = index, started
                return index, cluster.rf_switch_state.ravel(), response.offsets_hz, ratios
            if index == previous and started - last_seen < 0.25:
                break
        return None

    @staticmethod
    def _valid_bins(response):
        return np.isfinite(response.response) & (response.coherence >= 0.8) & (abs(response.response) > 0)

    @QtCore.pyqtSlot(list, list, QtCharts.QValueAxis)
    def updateCurves(self, amplitude, phase, power_axis):
        self._trim_history()
        key = tuple(stamp for stamp, _ in self._history)
        if key == getattr(self, "_drawn", None):
            return
        first_frame = not getattr(self, "_drawn", None)
        self._drawn = key
        if not key:
            for series in amplitude + phase:
                series.clear()
            return
        _, (reference, feeds, frequencies, latest) = self._history[-1]
        power_sum = np.zeros(latest.shape)
        phase_sum = np.zeros(latest.shape, complex)
        count = np.zeros(latest.shape, int)
        feeds = feeds.copy()
        for _, (anchor, routes, _, ratios) in reversed(self._history):
            # A channel ratio depends on both the sensor feed and the reference feed.
            if anchor != reference or routes[anchor] != feeds[reference]:
                continue
            # A missing sensor in the latest packet can use its last observed feed.
            missing = feeds == 255
            feeds[missing] = routes[missing]
            valid = np.isfinite(ratios) & (routes == feeds)[:, None]
            magnitudes = np.abs(ratios)
            power_sum += np.where(valid, magnitudes**2, 0)
            phase_sum += np.divide(ratios, magnitudes, out=np.zeros_like(ratios), where=valid)
            count += valid
        values = (10 * np.log10(np.maximum(power_sum / np.maximum(count, 1), 1e-30)), np.angle(phase_sum))
        valid = count > 0
        power = values[0][valid]
        if power.size:
            minimum, maximum = power.min(), power.max()
            lower, upper = power_axis.min(), power_axis.max()
            # Leave one tick of headroom on expansion. Shrink only after
            # three ticks are empty so noise near a tick cannot resize the plot.
            if first_frame or minimum < lower or minimum - lower >= 15:
                lower = 5 * np.floor(minimum / 5) - 5
            if first_frame or maximum > upper or upper - maximum >= 15:
                upper = 5 * np.ceil(maximum / 5) + 5
            if (lower, upper) != (power_axis.min(), power_axis.max()):
                power_axis.setRange(float(lower), float(upper))
        for series_set, curves in zip((amplitude, phase), values):
            for index, series in enumerate(series_set):
                mask = valid[index] & np.isfinite(curves[index])
                series.replace([QtCore.QPointF(float(f / 1e6), float(y)) for f, y in zip(frequencies[mask], curves[index, mask])])


if __name__ == "__main__":
    sys.exit(BLEChannelApplication(sys.argv).exec())
