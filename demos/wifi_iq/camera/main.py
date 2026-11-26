#!/usr/bin/env python
"""Display directions measured from WiFi packet IQ."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np
from PyQt6 import QtCore
from espargos.csi_processing import scale_csi_by_reported_gain
from demos.wifi.camera.backend import CSICamera, run_camera
from demos.wifi_iq.common.application import WiFiIQApplication


class WiFiIQCamera(CSICamera, WiFiIQApplication):
    qml_file = Path(__file__).with_name("Main.qml").resolve()
    level_fields = ("rx_gain",)
    power_unit = "dBFS"

    def _create_receiver(self, owner):
        receiver = super()._create_receiver(owner)
        self.controls.acquisitionChanged.connect(self._clear_measurements)
        return receiver

    def _polarization_channels(self):
        return self.controls.settings["channel"], 0

    def _on_update_app_state(self, newcfg):
        super()._on_update_app_state(newcfg)
        if (newcfg.get("beamformer", {}).get("polarization_mode") in ("incorporate", "show")
                and hasattr(self, "controls") and self.controls.settings and self.controls.settings["rf_switch"] != 4):
            self.controls.apply({"rf_switch": 4})

    def _process_args(self):
        super()._process_args()
        if self.get_initial_config("app", "beamformer", "polarization_mode") != "ignore":
            self.initial_config["wifi_iq"]["rf_switch"] = 4

    def normalize_channel(self, csi, levels):
        if self.controls.settings.get("gain_mode") == "auto" and self.appconfig.get("beamformer", "polarization_mode") != "ignore":
            return scale_csi_by_reported_gain(csi, levels[0], 0)
        return csi

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
