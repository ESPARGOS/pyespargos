#!/usr/bin/env python
"""Relative BLE channel amplitude and phase across its bandwidth."""
from pathlib import Path
import sys
import time
import numpy as np
from PyQt6 import QtCore, QtCharts

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from demos.ble.common.application import BLEMeasurementApplication


class BLEChannelApplication(BLEMeasurementApplication):
    DEFAULT_CONFIG = {
        **BLEMeasurementApplication.DEFAULT_CONFIG,
        "correct_timing": True,
        "nfft": 128,
    }
    QML_FILE = Path(__file__).with_name("Main.qml")

    def process_cluster(self, cluster):
        return self.controller.receiver.channel_response(cluster,
            calibrated=self.appconfig.get("calibrated"),
            correct_timing=self.appconfig.get("correct_timing"),
            nfft=int(self.appconfig.get("nfft")))

    @QtCore.pyqtSlot(list, list, QtCharts.QValueAxis)
    def updateCurves(self, amplitude, phase, power_axis):
        response = self._response
        if response is None or time.monotonic()-getattr(self,"_received_at",0) > 1:
            for series in amplitude+phase:
                series.clear()
            return
        h = response.response[0].reshape(8,-1)
        quality = response.coherence[0].reshape(8,-1)
        valid = np.isfinite(h) & (quality >= .8) & (abs(h) > 0)
        values = (20*np.log10(np.maximum(abs(h),1e-30)), np.angle(h))
        power = values[0][valid]
        if power.size:
            lower = 5 * np.floor(power.min()/5) - 5
            upper = 5 * np.ceil(power.max()/5) + 5
            power_axis.setRange(float(lower), float(upper))
        for series_set, curves in zip((amplitude,phase),values):
            for index, series in enumerate(series_set):
                mask = valid[index] & np.isfinite(curves[index])
                series.replace([QtCore.QPointF(float(f/1e6),float(y)) for f,y in
                                zip(response.offsets_hz[mask],curves[index,mask])])


if __name__ == "__main__":
    sys.exit(BLEChannelApplication(sys.argv).exec())
