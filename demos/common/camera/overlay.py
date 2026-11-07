"""Shared QML camera-overlay interface, independent of radio acquisition."""

import numpy as np
from PyQt6 import QtCore
from .images import OverlayImages


class CameraOverlayModel(QtCore.QObject):
    polarization_methods = ("FFT",)
    imageRevisionChanged = QtCore.pyqtSignal()
    visualizationSpaceChanged = QtCore.pyqtSignal()
    resolutionAzimuthChanged = QtCore.pyqtSignal()
    resolutionElevationChanged = QtCore.pyqtSignal()
    azimuthCorrectionChanged = QtCore.pyqtSignal()
    elevationCorrectionChanged = QtCore.pyqtSignal()
    polarizationVisibleChanged = QtCore.pyqtSignal()
    gridSpacingChanged = QtCore.pyqtSignal()
    polarizationSpeedChanged = QtCore.pyqtSignal()
    markersChanged = QtCore.pyqtSignal()

    def __init__(self, appconfig, parent=None, update_signal=None):
        super().__init__(parent)
        self.appconfig = appconfig
        self._markers = []
        self._images = OverlayImages()
        self._image_revision = 0
        signal = appconfig.updateAppState if update_signal is None else update_signal
        signal.connect(self._shared_update)

    def attach_to_engine(self, engine):
        engine.addImageProvider("radio-overlay", self._images)

    def publish_image(self, name, rgba):
        self._images.publish(name, rgba)
        self._image_revision += 1
        self.imageRevisionChanged.emit()

    @QtCore.pyqtProperty(str, notify=imageRevisionChanged)
    def spatialImageSource(self):
        return f"image://radio-overlay/spatial?{self._image_revision}"

    @QtCore.pyqtProperty(str, notify=imageRevisionChanged)
    def polarizationImageSource(self):
        return f"image://radio-overlay/polarization?{self._image_revision}"

    def publish_polarization(self, vertical, horizontal):
        # Two opaque RGB tiles retain all four Jones components without using
        # alpha for data (Qt Canvas textures use premultiplied alpha).
        components = np.concatenate([horizontal.T, vertical.T], axis=1)
        image = np.empty((*components.shape, 4), dtype=np.uint8)
        image[..., 0] = np.rint(np.clip((components.real + 1) / 2, 0, 1) * 255)
        image[..., 1] = np.rint(np.clip((components.imag + 1) / 2, 0, 1) * 255)
        image[..., 2] = 0
        image[..., 3] = 255
        self.publish_image("polarization", image)

    def publish_markers(self, directions, labels=None):
        """Mark emitter directions, given as direction cosines ``(u, v)``, in the view."""
        camera = self.appconfig.get("camera")
        visualization = self.appconfig.get("visualization")
        azimuth, elevation = np.radians(visualization["azimuth_correction"]), np.radians(visualization["elevation_correction"])
        markers = []
        for index, (u, v) in enumerate(directions):
            x, y, z = u, v, np.sqrt(max(1.0 - u * u - v * v, 0.0))
            label = f"{np.degrees(np.arctan2(u, z)):+.0f}° / {-np.degrees(np.arcsin(np.clip(v, -1.0, 1.0))):+.0f}°"
            if labels is not None:
                label = labels[index] + " · " + label
            if visualization["space"] == "camera":
                x = -x
                # Inverse of the overlay shader: undo the alignment rotations, then project
                x, z = np.cos(azimuth) * x - np.sin(azimuth) * z, np.sin(azimuth) * x + np.cos(azimuth) * z
                y, z = np.cos(elevation) * y - np.sin(elevation) * z, np.sin(elevation) * y + np.cos(elevation) * z
                if z <= 1e-6:
                    continue
                x = x / z / np.tan(np.radians(camera["fov_azimuth"]) / 2.0)
                y = y / z / np.tan(np.radians(camera["fov_elevation"]) / 2.0)
            x, y = 0.5 + 0.5 * x, 0.5 + 0.5 * y
            if camera["flip_overlay"]:
                x = 1.0 - x
            if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                markers.append({"x": round(float(x), 3), "y": round(float(y), 3), "label": label, "offscreen": False, "rotation": 0.0})
            elif visualization.get("edge_markers", False):
                markers.append({"x": round(float(np.clip(x, 0.035, 0.965)), 3), "y": round(float(np.clip(y, 0.035, 0.965)), 3), "label": "Outside view · " + label, "offscreen": True, "rotation": float(np.degrees(np.arctan2(y - 0.5, x - 0.5)))})
        if markers != self._markers:
            self._markers = markers
            self.markersChanged.emit()

    def _shared_update(self, changes):
        groups = {
            "beamformer": {
                "type": self.polarizationVisibleChanged,
                "polarization_mode": self.polarizationVisibleChanged,
                "grid_spacing": self.gridSpacingChanged,
                "polarization_speed": self.polarizationSpeedChanged,
                "resolution_azimuth": self.resolutionAzimuthChanged,
                "resolution_elevation": self.resolutionElevationChanged,
            },
            "visualization": {
                "space": self.visualizationSpaceChanged,
                "azimuth_correction": self.azimuthCorrectionChanged,
                "elevation_correction": self.elevationCorrectionChanged,
            },
        }
        for group, fields in groups.items():
            for key, signal in fields.items():
                if key in changes.get(group, {}):
                    signal.emit()

    @QtCore.pyqtProperty(int, notify=resolutionAzimuthChanged)
    def resolutionAzimuth(self):
        return int(self.appconfig.get("beamformer", "resolution_azimuth"))

    @QtCore.pyqtProperty(int, notify=resolutionElevationChanged)
    def resolutionElevation(self):
        return int(self.appconfig.get("beamformer", "resolution_elevation"))

    @QtCore.pyqtProperty(str, notify=visualizationSpaceChanged)
    def visualizationSpace(self):
        return str(self.appconfig.get("visualization", "space"))

    @QtCore.pyqtProperty(float, notify=azimuthCorrectionChanged)
    def azimuth_correction(self):
        return float(self.appconfig.get("visualization", "azimuth_correction"))

    @QtCore.pyqtProperty(float, notify=elevationCorrectionChanged)
    def elevation_correction(self):
        return float(self.appconfig.get("visualization", "elevation_correction"))

    @QtCore.pyqtProperty(bool, notify=polarizationVisibleChanged)
    def polarizationVisible(self):
        config = self.appconfig.get("beamformer")
        return config.get("type") in self.polarization_methods and config.get("polarization_mode") == "show"

    @QtCore.pyqtProperty(float, notify=gridSpacingChanged)
    def gridSpacing(self):
        return float(self.appconfig.get("beamformer").get("grid_spacing", 24))

    @QtCore.pyqtProperty(float, notify=polarizationSpeedChanged)
    def polarizationSpeed(self):
        return float(self.appconfig.get("beamformer").get("polarization_speed", 0))

    @QtCore.pyqtProperty(list, notify=markersChanged)
    def markers(self):
        return self._markers

    @QtCore.pyqtProperty(int, constant=True)
    def renderStyle(self):
        return 0
