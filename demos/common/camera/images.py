"""Small immutable RGBA textures shared by the camera overlays."""

import threading
import numpy as np
from PyQt6 import QtGui, QtQuick


class OverlayImages(QtQuick.QQuickImageProvider):
    def __init__(self):
        super().__init__(QtQuick.QQuickImageProvider.ImageType.Image)
        self._lock = threading.Lock()
        blank = QtGui.QImage(1, 1, QtGui.QImage.Format.Format_RGBA8888)
        blank.fill(QtGui.QColor(0, 0, 0, 255))
        no_polarization = blank.copy()
        no_polarization.fill(QtGui.QColor(128, 128, 0, 255))
        self._images = {"spatial": blank, "polarization": no_polarization}

    def publish(self, name, rgba):
        rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
        if rgba.ndim != 3 or rgba.shape[-1] != 4:
            raise ValueError("Overlay image must have shape (height, width, 4)")
        height, width = rgba.shape[:2]
        # Own the bytes: the NumPy buffer may disappear before Qt uploads it.
        image = QtGui.QImage(rgba.data, width, height, width * 4, QtGui.QImage.Format.Format_RGBA8888).copy()
        with self._lock:
            self._images[name] = image

    def requestImage(self, identifier, requestedSize):
        with self._lock:
            image = self._images[identifier.split("?", 1)[0]]
        return image, image.size()
