"""Reusable rolling waterfall rendering for IQ demos.

The signal analyzer renders spectra into a QML image provider.  This module owns the thread-safe rolling RGBA buffer and the small
amount of spectrum-to-pixel policy shared by those displays; acquisition and
FFT construction deliberately stay with each demo.
"""

from __future__ import annotations

import threading

import numpy as np

__all__ = [
    "DB_MAX",
    "DB_MIN",
    "WaterfallImageProvider",
    "max_pool_to_width",
    "power_db_row",
    "viridis_rgba",
]
import PyQt6.QtGui
import PyQt6.QtQml
import PyQt6.QtQuick

DB_MIN = -90.0
DB_MAX = 0.0

# Compact viridis approximation.  Interpolation is sufficient for a live
# diagnostic waterfall and avoids making the reusable provider depend on
# matplotlib (the signal analyzer still uses matplotlib for its phase map).
_VIRIDIS_STOPS = np.asarray(
    [
        (0.267004, 0.004874, 0.329415),
        (0.229739, 0.322361, 0.545706),
        (0.127568, 0.566949, 0.550556),
        (0.369214, 0.788888, 0.382914),
        (0.993248, 0.906157, 0.143936),
    ],
    dtype=np.float32,
)


def max_pool_to_width(values: np.ndarray, width: int) -> np.ndarray:
    """Resize one spectrum to ``width`` pixels, preserving narrow peaks."""

    values = np.asarray(values)
    width = max(1, int(width))
    count = values.shape[-1]
    if count == width:
        return values
    if width < count:
        edges = (np.arange(width) * count) // width
        return np.maximum.reduceat(values, edges, axis=-1)
    return values[..., (np.arange(width) * count) // width]


def viridis_rgba(values: np.ndarray, vmin: float = DB_MIN, vmax: float = DB_MAX) -> np.ndarray:
    """Map scalar values to uint8 viridis RGBA pixels."""

    values = np.asarray(values, dtype=np.float32)
    normalized = np.clip((values - vmin) / max(vmax - vmin, 1e-12), 0.0, 1.0)
    index = normalized * (_VIRIDIS_STOPS.shape[0] - 1)
    low = np.floor(index).astype(np.intp)
    high = np.ceil(index).astype(np.intp)
    fraction = (index - low)[..., np.newaxis]
    rgb = _VIRIDIS_STOPS[low] * (1.0 - fraction) + _VIRIDIS_STOPS[high] * fraction
    alpha = np.ones((*rgb.shape[:-1], 1), dtype=np.float32)
    return np.asarray(np.concatenate((rgb, alpha), axis=-1) * 255.0, dtype=np.uint8)


def power_db_row(values_db: np.ndarray, width: int, vmin: float = DB_MIN, vmax: float = DB_MAX) -> np.ndarray:
    """Pool one dB spectrum to ``width`` and return a viridis RGBA row."""

    return viridis_rgba(max_pool_to_width(values_db, width), vmin=vmin, vmax=vmax)


class WaterfallImageProvider(PyQt6.QtQuick.QQuickImageProvider):
    """Thread-safe rolling RGBA image exposed through QML ``image://``."""

    def __init__(self, width: int, height: int, floor_db: float = DB_MIN):
        super().__init__(PyQt6.QtQml.QQmlImageProviderBase.ImageType.Image)
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.floor_db = float(floor_db)
        self.lock = threading.Lock()
        self.clear()

    def clear(self):
        floor = viridis_rgba(np.asarray(self.floor_db, dtype=np.float32))
        with self.lock:
            self.data = np.empty((self.height, self.width, 4), dtype=np.uint8)
            self.data[:] = floor

    def resize(self, width: int, height: int | None = None):
        width = max(1, int(width))
        height = self.height if height is None else max(1, int(height))
        if width == self.width and height == self.height:
            return
        self.width = width
        self.height = height
        self.clear()

    def requestImage(self, image_id, requested_size):
        del image_id, requested_size
        with self.lock:
            # QImage initially borrows the array; take its owned snapshot
            # before releasing the lock so a producer cannot tear the image.
            image = PyQt6.QtGui.QImage(
                self.data.data,
                self.data.shape[1],
                self.data.shape[0],
                PyQt6.QtGui.QImage.Format.Format_RGBA8888,
            ).copy()
        return image, image.size()

    def add_rows(self, rows: np.ndarray):
        """Append chronological rows, displaying the newest row at the top."""
        rows = np.asarray(rows, dtype=np.uint8)
        if rows.ndim == 2:
            rows = rows[np.newaxis, ...]
        count = min(rows.shape[0], self.height)
        if count == 0:
            return
        with self.lock:
            if rows.shape[1:] != self.data.shape[1:]:
                return  # resize raced the producer; discard this update
            self.data[count:, :, :] = self.data[:-count, :, :]
            self.data[:count, :, :] = rows[-count:][::-1]

    def add_power_db(self, values_db: np.ndarray, vmin: float = DB_MIN, vmax: float = DB_MAX):
        self.add_rows(power_db_row(values_db, self.width, vmin=vmin, vmax=vmax))
