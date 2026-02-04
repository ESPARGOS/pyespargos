#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin, ConfigManager

from matplotlib import colormaps
import numpy as np
import espargos

import PyQt6.QtCore

BEAMSPACE_OVERSAMPLING = 16
DELAY_OVERSAMPLING = 10


class AzimuthDelayApp(BacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    DEFAULT_CONFIG = {
        "delay_min": -3,
        "delay_max": 5,
    }

    dataChanged = PyQt6.QtCore.pyqtSignal(list)
    configChanged = PyQt6.QtCore.pyqtSignal()
    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()

    def __init__(self, argv):
        super().__init__(
            argv,
        )

        # App-specific configuration
        self.appconfig = ConfigManager(self.get_initial_config("app"), parent=self)
        self.appconfig.updateAppState.connect(self.onConfigUpdate)

        # Subscribe to preamble format changes
        self.genericconfig.updateAppState.connect(self._on_preamble_format_changed)

        # Initialize pool and backlog
        self.initialize_pool()
        self.initComplete.connect(self.onInitComplete)

        self._delay_size = (self.appconfig.get("delay_max") - self.appconfig.get("delay_min")) * DELAY_OVERSAMPLING + 1
        self._angle_size = self.n_cols * BEAMSPACE_OVERSAMPLING + 1

        self.data = []

    def _on_preamble_format_changed(self, newcfg):
        self.preambleFormatChanged.emit()
        self.genericconfig.updateAppStateHandled.emit()

    def onInitComplete(self):
        self.updateDelaySize()

    def onConfigUpdate(self, newconfig):
        if "delay_min" in newconfig or "delay_max" in newconfig:
            self.updateDelaySize()
            self.configChanged.emit()
        self.appconfig.updateAppStateHandled.emit()

    def updateDelaySize(self):
        delay_min = self.appconfig.get("delay_min")
        delay_max = self.appconfig.get("delay_max")
        self._delay_size = (delay_max - delay_min) * DELAY_OVERSAMPLING + 1

    def exec(self):
        qml_file = pathlib.Path(__file__).resolve().parent / "azimuth-delay.qml"
        self.initialize_qml(qml_file, context_props={"appconfig": self.appconfig})

        if not self.engine.rootObjects():
            return -1

        return super().exec()

    def colormap(self, data):
        colormap = colormaps.get_cmap("viridis")
        norm_data = data / np.max(data) if np.max(data) != 0 else data
        color_data = colormap(np.transpose(norm_data))
        np_data = color_data * 255
        self._angle_size = np_data.shape[1]
        self._delay_size = np_data.shape[0]
        image_data_list = np_data.flatten().tolist()
        return image_data_list

    def get_subcarrier_count(self):
        preamble_format = self.genericconfig.get("preamble_format")
        if preamble_format == "lltf":
            return espargos.csi.LEGACY_COEFFICIENTS_PER_CHANNEL
        elif preamble_format == "ht20":
            return espargos.csi.HT_COEFFICIENTS_PER_CHANNEL
        else:
            return espargos.csi.HT_COEFFICIENTS_PER_CHANNEL * 2 + espargos.csi.HT40_GAP_SUBCARRIERS

    @PyQt6.QtCore.pyqtSlot()
    def update_data(self):
        preamble_format = self.genericconfig.get("preamble_format")

        try:
            csi = self.backlog.get(preamble_format)
        except ValueError:
            print(f"Requested CSI key {preamble_format} not in backlog")
            return

        # If any value is NaN skip this update (happens if received frame were not of expected type)
        if np.isnan(csi).any():
            return

        # If backlog is empty, skip update
        if csi.size == 0:
            return

        if preamble_format == "ht20":
            espargos.util.interpolate_ht20ltf_gap(csi)
        elif preamble_format == "ht40":
            espargos.util.interpolate_ht40ltf_gap(csi)

        # Remove STO from CSI
        espargos.util.remove_mean_sto(csi)

        delay_min = self.appconfig.get("delay_min")
        delay_max = self.appconfig.get("delay_max")

        subcarriers = csi.shape[-1]

        # Build combined array CSI
        csi_largearray = espargos.util.build_combined_array_csi(self.indexing_matrix, csi)
        # csi_largearray shape: (backlog_depth, n_rows, n_cols, subcarriers)

        # Sum over rows (beamform vertically)
        csi = np.sum(csi_largearray, axis=1)  # shape: (backlog_depth, n_cols, subcarriers)

        csi_padded = np.zeros(
            (
                csi.shape[0],
                self.n_cols * BEAMSPACE_OVERSAMPLING,
                subcarriers * DELAY_OVERSAMPLING,
            ),
            dtype=csi.dtype,
        )
        csi_padded[:, : csi.shape[1], : csi.shape[2]] = csi
        csi_padded = np.roll(
            np.fft.fft(csi_padded, axis=1),
            (self.n_cols * BEAMSPACE_OVERSAMPLING) // 2,
            axis=1,
        )  # beamspace
        csi_padded = np.roll(np.fft.ifft(csi_padded, axis=2), -delay_min * DELAY_OVERSAMPLING, axis=2)  # from frequency to delay domain
        csi_padded = np.abs(csi_padded)
        csi_padded = np.sum(csi_padded, axis=0)  # sum of all backlog samples
        self.data = csi_padded[:, : (delay_max - delay_min) * DELAY_OVERSAMPLING + 1]  # only relevant delays
        self.data = np.append(
            self.data,
            self.data[0, :].reshape(1, (delay_max - delay_min) * DELAY_OVERSAMPLING + 1),
            axis=0,
        )  # beamspace -pi identical to pi
        self.image_data = self.colormap(self.data)
        self.dataChanged.emit(self.image_data)

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=configChanged)
    def delaySize(self):
        return self._delay_size

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=configChanged)
    def angleSize(self):
        return self._angle_size

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=configChanged)
    def delayMin(self):
        return self.appconfig.get("delay_min")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=configChanged)
    def delayMax(self):
        return self.appconfig.get("delay_max")


app = AzimuthDelayApp(sys.argv)
sys.exit(app.exec())
