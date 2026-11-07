"""QML-facing state and rendering policy for the IQ camera demo."""

from __future__ import annotations

import numpy as np
import PyQt6.QtCore
from demos.common.camera.overlay import CameraOverlayModel


class IQCameraOverlay(CameraOverlayModel):
    waterfallGenerationChanged = PyQt6.QtCore.pyqtSignal()
    waterfallVisibleChanged = PyQt6.QtCore.pyqtSignal()
    spectrumStatusChanged = PyQt6.QtCore.pyqtSignal()
    frequencySpanChanged = PyQt6.QtCore.pyqtSignal()
    captureStatusChanged = PyQt6.QtCore.pyqtSignal()
    displayModeChanged = PyQt6.QtCore.pyqtSignal()

    def __init__(self, appconfig, parent=None, update_signal=None):
        super().__init__(appconfig, parent, update_signal)
        self._waterfall_generation = 0
        self._spectrum_status = "Waiting for signals"
        self._capture_status = "Starting"
        self._frequency_low_mhz = 0.0
        self._frequency_high_mhz = 0.0
        self._markers = []
        signal = self.appconfig.updateAppState if update_signal is None else update_signal
        signal.connect(self._on_update_app_state)

    def publish_emission(self, power_dbfs, low_mhz, high_mhz, purity):
        """Describe the strongest emission since the last update (``low_mhz`` None: nothing detected)."""
        status = "No signal above threshold" if low_mhz is None else f"{low_mhz:.1f}–{high_mhz:.1f} MHz · {power_dbfs:.0f} dBFS · purity {purity:.2f}"
        if status != self._spectrum_status:
            self._spectrum_status = status
            self.spectrumStatusChanged.emit()

    def publish_spatial_spectrum(self, power, reference):
        """Show ``power`` on a dB scale whose top is ``reference``."""
        db = 10.0 * np.log10(np.maximum(np.asarray(power, dtype=np.float64) / max(reference, 1e-30), 1e-15))
        dynamic_range = max(1.0, float(self.appconfig.get("visualization", "dynamic_range_db")))
        intensity = np.clip((db + dynamic_range) / dynamic_range, 0.0, 1.0) ** 2
        image = np.zeros(4 * power.size, dtype=np.uint8)
        image[1::4] = np.asarray(np.swapaxes(intensity, 0, 1).ravel() * 255.0, dtype=np.uint8)
        image[3::4] = 255
        self.publish_image("spatial", image.reshape(power.shape[1], power.shape[0], 4))

    def publish_waterfall(self, low_mhz, high_mhz):
        span_changed = low_mhz != self._frequency_low_mhz or high_mhz != self._frequency_high_mhz
        self._frequency_low_mhz = float(low_mhz)
        self._frequency_high_mhz = float(high_mhz)
        if span_changed:
            self.frequencySpanChanged.emit()
        self._waterfall_generation += 1
        self.waterfallGenerationChanged.emit()

    def set_capture_status(self, status):
        status = str(status)
        if status != self._capture_status:
            self._capture_status = status
            self.captureStatusChanged.emit()

    @PyQt6.QtCore.pyqtSlot(dict)
    def _on_update_app_state(self, changes):
        if "waterfall" in changes.get("visualization", {}):
            self.waterfallVisibleChanged.emit()
        if "display_mode" in changes.get("visualization", {}):
            self.displayModeChanged.emit()

    @PyQt6.QtCore.pyqtProperty(str, notify=displayModeChanged)
    def displayMode(self):
        return self.appconfig.get("visualization", "display_mode")

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def renderStyle(self):
        return 1

    @PyQt6.QtCore.pyqtProperty(int, notify=waterfallGenerationChanged)
    def waterfallGeneration(self):
        return self._waterfall_generation

    @PyQt6.QtCore.pyqtProperty(bool, notify=waterfallVisibleChanged)
    def waterfallVisible(self):
        return bool(self.appconfig.get("visualization", "waterfall"))

    @PyQt6.QtCore.pyqtProperty(str, notify=spectrumStatusChanged)
    def spectrumStatus(self):
        return self._spectrum_status

    @PyQt6.QtCore.pyqtProperty(str, notify=captureStatusChanged)
    def captureStatus(self):
        return self._capture_status

    @PyQt6.QtCore.pyqtProperty(float, notify=frequencySpanChanged)
    def frequencyLowMhz(self):
        return self._frequency_low_mhz

    @PyQt6.QtCore.pyqtProperty(float, notify=frequencySpanChanged)
    def frequencyHighMhz(self):
        return self._frequency_high_mhz
