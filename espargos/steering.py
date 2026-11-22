"""Spatial weights and geometric beam steering."""

from dataclasses import dataclass
import math
from types import MappingProxyType
import numpy as np
from .calibration import Antenna
from .constants import SPEED_OF_LIGHT
from .waveforms import CW, _finite

__all__ = ["Beamspace", "Weights"]


@dataclass(frozen=True)
class Beamspace:
    """Angles in degrees, viewed outward from the array; broadside is zero."""

    azimuth_deg: float = 0
    elevation_deg: float = 0

    def coefficients(self, position, frequency, waveform):
        az = _finite(self.azimuth_deg, "azimuth_deg")
        el = _finite(self.elevation_deg, "elevation_deg")
        if abs(az) > 90 or abs(el) > 90:
            raise ValueError("Beamspace angles must lie within ±90 degrees")
        if position is None:
            raise ValueError("Beamspace steering requires antenna geometry; use Pool.from_config()")
        az, el = math.radians(az), math.radians(el)
        delay = (position[0] * math.sin(az) * math.cos(el) + position[1] * math.sin(el)) / SPEED_OF_LIGHT
        if isinstance(waveform, CW):
            frequency += waveform.offset_hz
        return np.exp(-2j * np.pi * frequency * delay)


@dataclass(frozen=True)
class Weights:
    """Per-antenna complex weights.

    Unspecified antennas use unit weight.
    """

    values: object

    def __post_init__(self):
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        for a, value in self.values.items():
            if not isinstance(a, Antenna) or not np.isfinite(complex(value)):
                raise ValueError("Weights must map Antenna objects to finite complex values")


def antenna_weight(steering, antenna, position, frequency_hz, waveform):
    """Resolve one contribution on the calibrated sample grid."""
    if isinstance(steering, Beamspace):
        return steering.coefficients(position, frequency_hz, waveform)
    if isinstance(steering, Weights):
        return steering.values.get(antenna, 1)
    return 1
