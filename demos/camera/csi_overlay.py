"""QML-facing state and rendering policy for the WiFi CSI camera demo."""

from __future__ import annotations

import matplotlib.colors
import numpy as np
import PyQt6.QtCore


class CSIOverlay(PyQt6.QtCore.QObject):
    receiverPowerChanged = PyQt6.QtCore.pyqtSignal(float)
    receiverPowerUnitChanged = PyQt6.QtCore.pyqtSignal()
    activeAntennasChanged = PyQt6.QtCore.pyqtSignal(float)
    beamspacePowerImagedataChanged = PyQt6.QtCore.pyqtSignal(list)
    polarizationImagedataChanged = PyQt6.QtCore.pyqtSignal(list)
    macListChanged = PyQt6.QtCore.pyqtSignal(list)
    visualizationSpaceChanged = PyQt6.QtCore.pyqtSignal()
    polarizationVisibleChanged = PyQt6.QtCore.pyqtSignal()
    gridSpacingChanged = PyQt6.QtCore.pyqtSignal()
    resolutionAzimuthChanged = PyQt6.QtCore.pyqtSignal()
    resolutionElevationChanged = PyQt6.QtCore.pyqtSignal()
    macListEnabledChanged = PyQt6.QtCore.pyqtSignal()
    azimuthCorrectionChanged = PyQt6.QtCore.pyqtSignal()
    elevationCorrectionChanged = PyQt6.QtCore.pyqtSignal()

    def __init__(self, appconfig, parent=None, update_signal=None):
        super().__init__(parent)
        self.appconfig = appconfig
        self.mean_receiver_power = -np.inf
        self.receiver_power_unit = "dB"
        self.mean_active_antennas = 0.0
        self.recent_macs = set()
        signal = self.appconfig.updateAppState if update_signal is None else update_signal
        signal.connect(self._on_update_app_state)

    def publish_receiver_statistics(self, power, unit, active_receivers):
        self.mean_receiver_power = float(power)
        if self.receiver_power_unit != unit:
            self.receiver_power_unit = unit
            self.receiverPowerUnitChanged.emit()
        self.mean_active_antennas = float(active_receivers)
        self.receiverPowerChanged.emit(self.mean_receiver_power)
        self.activeAntennasChanged.emit(self.mean_active_antennas)

    def publish_mac_addresses(self, addresses):
        addresses = set(addresses)
        if addresses != self.recent_macs:
            self.recent_macs = addresses
            self.macListChanged.emit(list(self.recent_macs))

    def publish_polarization(self, vertical, horizontal):
        image = np.zeros(vertical.size * 4, dtype=np.uint8)
        image[0::4] = np.clip(np.swapaxes((vertical + 1.0) / 2.0, 0, 1).ravel(), 0, 1) * 255
        image[1::4] = np.clip(np.swapaxes((horizontal.real + 1.0) / 2.0, 0, 1).ravel(), 0, 1) * 255
        image[2::4] = np.clip(np.swapaxes((horizontal.imag + 1.0) / 2.0, 0, 1).ravel(), 0, 1) * 255
        image[3::4] = 255
        self.polarizationImagedataChanged.emit(image.tolist())

    def publish_spatial_spectrum(self, power, frequency_space=None):
        power = np.asarray(power)
        if self.appconfig.get("visualization", "overlay") == "Power":
            db_beamspace = 10 * np.log10(power + 1e-6)
            normalized = np.clip((db_beamspace - np.max(db_beamspace) + 15) / 15, 0, 1)
            colors = self._viridis(normalized)
            alpha = np.ones((*colors.shape[:2], 1))
            image = np.asarray(np.swapaxes(np.clip(np.concatenate((colors, alpha), axis=-1), 0, 1), 0, 1).ravel() * 255, dtype=np.uint8)
        else:
            visualized_power = power**3
            if self.appconfig.get("visualization", "manual_exposure"):
                value_range = {
                    "MUSIC": 1e1,
                    "MVDR": 1e4,
                    "FFT": 1e6,
                    "Bartlett": 1e6,
                }[self.appconfig.get("beamformer", "type")]
                exposure = self.appconfig.get("visualization", "exposure")
                color_value = visualized_power / value_range * (10 ** (exposure / 0.1) + 1e-15)
            else:
                color_value = visualized_power / (np.max(visualized_power) + 1e-15)

            if self.appconfig.get("beamformer", "colorize_delay"):
                if frequency_space is None:
                    raise ValueError("frequency_space is required for delay colorization")
                if frequency_space.ndim == 3:
                    frequency_space = frequency_space[np.newaxis, ...]
                weighted_delay_phase = np.sum(
                    frequency_space[..., 1:] * np.conj(frequency_space[..., :-1]),
                    axis=(0, -1),
                )
                delay_by_beam = np.angle(weighted_delay_phase)
                mean_delay = np.angle(np.sum(weighted_delay_phase))
                hsv = np.zeros((frequency_space.shape[1], frequency_space.shape[2], 3))
                hsv[:, :, 0] = (
                    np.clip(
                        (delay_by_beam - mean_delay) / self.appconfig.get("beamformer", "max_delay"),
                        0,
                        1,
                    )
                    + 1 / 3
                ) % 1.0
                hsv[:, :, 1] = 0.8
                hsv[:, :, 2] = color_value
                rgb = matplotlib.colors.hsv_to_rgb(hsv)
                alpha = np.ones((*rgb.shape[:2], 1))
                image = np.asarray(np.swapaxes(np.clip(np.concatenate((rgb, alpha), axis=-1), 0, 1), 0, 1).ravel() * 255, dtype=np.uint8)
            else:
                image = np.zeros(4 * power.size, dtype=np.uint8)
                image[1::4] = np.clip(np.swapaxes(color_value, 0, 1).ravel(), 0, 1) * 255
                image[3::4] = 255

        self.beamspacePowerImagedataChanged.emit(image.tolist())

    @staticmethod
    def _viridis(values):
        colormap = np.asarray(
            [
                (0.267004, 0.004874, 0.329415),
                (0.229739, 0.322361, 0.545706),
                (0.127568, 0.566949, 0.550556),
                (0.369214, 0.788888, 0.382914),
                (0.993248, 0.906157, 0.143936),
                (0.993248, 0.906157, 0.143936),
            ]
        )
        index = values * (len(colormap) - 1)
        low = np.floor(index).astype(int)
        high = np.ceil(index).astype(int)
        fraction = index - low
        return colormap[low] * (1 - fraction[:, :, np.newaxis]) + colormap[high] * fraction[:, :, np.newaxis]

    @PyQt6.QtCore.pyqtSlot(dict)
    def _on_update_app_state(self, newcfg):
        receiver_cfg = newcfg.get("receiver", {}) if isinstance(newcfg, dict) else {}
        if isinstance(receiver_cfg, dict) and "mac_list_enabled" in receiver_cfg:
            self.macListEnabledChanged.emit()

        beamformer_cfg = newcfg.get("beamformer", {}) if isinstance(newcfg, dict) else {}
        if isinstance(beamformer_cfg, dict):
            if "polarization_mode" in beamformer_cfg or "type" in beamformer_cfg:
                self.polarizationVisibleChanged.emit()
            if "grid_spacing" in beamformer_cfg:
                self.gridSpacingChanged.emit()
            if "resolution_azimuth" in beamformer_cfg:
                self.resolutionAzimuthChanged.emit()
            if "resolution_elevation" in beamformer_cfg:
                self.resolutionElevationChanged.emit()

        visualization_cfg = newcfg.get("visualization", {}) if isinstance(newcfg, dict) else {}
        if isinstance(visualization_cfg, dict):
            if "space" in visualization_cfg:
                self.visualizationSpaceChanged.emit()
            if "azimuth_correction" in visualization_cfg:
                self.azimuthCorrectionChanged.emit()
            if "elevation_correction" in visualization_cfg:
                self.elevationCorrectionChanged.emit()

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=resolutionAzimuthChanged)
    def resolutionAzimuth(self):
        return self.appconfig.get("beamformer", "resolution_azimuth")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=resolutionElevationChanged)
    def resolutionElevation(self):
        return self.appconfig.get("beamformer", "resolution_elevation")

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=visualizationSpaceChanged)
    def visualizationSpace(self):
        return self.appconfig.get("visualization", "space")

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=receiverPowerChanged)
    def receiverPower(self):
        return self.mean_receiver_power

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=receiverPowerUnitChanged)
    def receiverPowerUnit(self):
        return self.receiver_power_unit

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=activeAntennasChanged)
    def activeAntennas(self):
        return self.mean_active_antennas

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=macListEnabledChanged)
    def macListEnabled(self):
        return self.appconfig.get("receiver", "mac_list_enabled")

    @PyQt6.QtCore.pyqtProperty(list, constant=False, notify=macListChanged)
    def macList(self):
        return list(self.recent_macs)

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=polarizationVisibleChanged)
    def polarizationVisible(self):
        return self.appconfig.get("beamformer", "type") == "FFT" and self.appconfig.get("beamformer", "polarization_mode") == "show"

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=gridSpacingChanged)
    def gridSpacing(self):
        return float(self.appconfig.get("beamformer", "grid_spacing"))

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=azimuthCorrectionChanged)
    def azimuth_correction(self):
        return float(self.appconfig.get("visualization", "azimuth_correction"))

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=elevationCorrectionChanged)
    def elevation_correction(self):
        return float(self.appconfig.get("visualization", "elevation_correction"))
