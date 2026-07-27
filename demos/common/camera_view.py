"""Composable video-camera state for QML demos."""

from __future__ import annotations

import PyQt6.QtCore
import PyQt6.QtMultimedia

from . import video_camera


class CameraView(PyQt6.QtCore.QObject):
    """Own video input and camera-only presentation state.

    The object has no knowledge of receivers, measurements, overlays, or
    spatial processing. Applications expose it to QML through composition.
    """

    enabledChanged = PyQt6.QtCore.pyqtSignal()
    flipChanged = PyQt6.QtCore.pyqtSignal()
    fovAzimuthChanged = PyQt6.QtCore.pyqtSignal()
    fovElevationChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "enable": True,
        "flip": False,
        "format": None,
        "device": None,
        "fov_azimuth": 72,
        "fov_elevation": 41,
    }

    def __init__(self, appconfig, parent=None, update_signal=None):
        super().__init__(parent)
        self.appconfig = appconfig
        self.video_camera = None
        signal = self.appconfig.updateAppState if update_signal is None else update_signal
        signal.connect(self._on_update_app_state)

    def initialize(self):
        """Create the configured camera object and return it for QML."""

        if self.enabled:
            self.video_camera = video_camera.VideoCamera(
                self.appconfig.get("camera", "device"),
                self.appconfig.get("camera", "format"),
            )
            self.appconfig.set(
                {
                    "camera": {
                        "device": self.video_camera.getDevice(),
                        "format": self.video_camera.getFormat(),
                    }
                }
            )
        else:
            self.video_camera = video_camera.DummyVideoCamera()
        return self.video_camera

    def start(self):
        if self.enabled and self.video_camera is not None:
            self.video_camera.setFocusMode(PyQt6.QtMultimedia.QCamera.FocusMode.FocusModeManual)
            self.video_camera.start()

    def stop(self):
        if self.video_camera is not None:
            self.video_camera.stop()

    @PyQt6.QtCore.pyqtSlot(dict)
    def _on_update_app_state(self, newcfg):
        camera_cfg = newcfg.get("camera", {}) if isinstance(newcfg, dict) else {}
        if not isinstance(camera_cfg, dict):
            return

        if "enable" in camera_cfg:
            self.enabledChanged.emit()
        if self.enabled and self.video_camera is not None and "device" in camera_cfg:
            self.video_camera.setDevice(camera_cfg.get("device"))
        if self.enabled and self.video_camera is not None and "format" in camera_cfg:
            self.video_camera.setFormat(camera_cfg.get("format"))
        if "flip" in camera_cfg:
            self.flipChanged.emit()
        if "fov_azimuth" in camera_cfg:
            self.fovAzimuthChanged.emit()
        if "fov_elevation" in camera_cfg:
            self.fovElevationChanged.emit()

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=enabledChanged)
    def enabled(self):
        return bool(self.appconfig.get("camera", "enable"))

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=flipChanged)
    def flip(self):
        return bool(self.appconfig.get("camera", "flip"))

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=fovAzimuthChanged)
    def fovAzimuth(self):
        return int(self.appconfig.get("camera", "fov_azimuth"))

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=fovElevationChanged)
    def fovElevation(self):
        return int(self.appconfig.get("camera", "fov_elevation"))
