#!/usr/bin/env python
"""Display native WiFi CSI over a live camera image."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from PyQt6 import QtCore
from espargos.csi_processing import scale_csi_by_reported_gain
from demos.wifi.camera.backend import CSICamera, run_camera
from demos.wifi.common.application import ESPARGOSWiFiApplication


class CSICameraDemo(CSICamera, ESPARGOSWiFiApplication):
    qml_file = Path(__file__).with_name("Main.qml").resolve()
    level_fields = ("rssi", "rx_gain", "fft_gain")
    power_unit = "dBm"

    @staticmethod
    def received_power(csi, levels):
        return levels[0]

    def normalize_channel(self, csi, levels):
        return scale_csi_by_reported_gain(csi, *levels[1:])

    @QtCore.pyqtSlot(str)
    def setMacFilter(self, mac):
        self.receiver.set_mac_filter({"enable": True, "mac": mac})
        self.pooldrawer.configManager().set({"mac_filter": {"enable": True, "mac": mac}})

    @QtCore.pyqtSlot()
    def clearMacFilter(self):
        self.receiver.clear_mac_filter()
        self.pooldrawer.configManager().set({"mac_filter": {"enable": False}})


EspargosDemoCamera = CSICameraDemo


def main(argv=None):
    return run_camera(CSICameraDemo, argv)


if __name__ == "__main__":
    sys.exit(main())
