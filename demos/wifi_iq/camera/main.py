#!/usr/bin/env python
"""Display directions measured from WiFi packet IQ."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np
from PyQt6 import QtCore
from demos.wifi.camera.backend import CSICamera, run_camera
from demos.wifi_iq.common.application import WiFiIQApplication


class WiFiIQCamera(CSICamera, WiFiIQApplication):
    qml_file = Path(__file__).with_name("Main.qml").resolve()
    level_fields = ()
    power_unit = "dBFS"

    def _create_receiver(self, owner):
        if self.appconfig.get("beamformer", "polarization_mode") != "ignore":
            raise ValueError("The WiFi IQ camera uses one antenna feed")
        receiver = super()._create_receiver(owner)
        self.controls.acquisitionChanged.connect(self._clear_measurements)
        return receiver

    def _clear_measurements(self):
        if hasattr(self, "overlay"):
            width, height = self.steering_vectors_2d.shape[-2:]
            self.overlay.publish_image("spatial", np.zeros((height, width, 4), np.uint8))
            self.overlay.publish_receiver_statistics(-np.inf, self.power_unit, 0)

    def received_power(self, csi, levels):
        count = np.isfinite(csi).sum(axis=-1)
        power = np.nansum(np.abs(self.adc_normalized_channel(csi))**2, axis=-1)
        if csi.shape[-1] == 35:
            power /= np.maximum(count, 1)
        power[count == 0] = np.nan
        return 10 * np.log10(np.maximum(power, 1e-20))

    @QtCore.pyqtSlot(str)
    def setMacFilter(self, mac):
        self.controls.setMacFilter(mac)

    @QtCore.pyqtSlot()
    def clearMacFilter(self):
        self.controls.setMacFilter("")


if __name__ == "__main__":
    sys.exit(run_camera(WiFiIQCamera, sys.argv))
