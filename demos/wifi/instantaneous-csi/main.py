#!/usr/bin/env python
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np
from demos.wifi.common.instantaneous_csi import InstantaneousCSI
from demos.wifi.common.application import ESPARGOSWiFiApplication
from espargos.csi_processing import scale_csi_by_reported_gain


class EspargosDemoInstantaneousCSI(InstantaneousCSI, ESPARGOSWiFiApplication):
    qml_file = Path(__file__).with_name("Main.qml").resolve()
    level_fields = ("rx_gain", "fft_gain", "lltf_8bit_mode")

    def scale_channel(self, csi, levels):
        rx_gain, fft_gain, _ = levels
        return scale_csi_by_reported_gain(csi, np.nan_to_num(rx_gain), np.nan_to_num(fft_gain))

    def _constellation_axis_limit(self, csi_key: str, csi_backlog: np.ndarray, levels) -> int:
        if csi_key != "lltf":
            return 128

        valid_lltf_sensors = np.any(np.isfinite(csi_backlog), axis=-1)
        lltf_8bit_mode = levels[2][valid_lltf_sensors]
        if lltf_8bit_mode.size == 0:
            return 128
        if np.count_nonzero(lltf_8bit_mode) < np.count_nonzero(~lltf_8bit_mode):
            return 2048
        return 128


if __name__ == "__main__":
    sys.exit(EspargosDemoInstantaneousCSI(sys.argv).exec())
