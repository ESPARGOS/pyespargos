"""Spatial weights and geometric beam steering."""

from dataclasses import dataclass
from types import MappingProxyType
import numpy as np
from .calibration import Antenna
from .constants import SPEED_OF_LIGHT
from .waveforms import CW, SAMPLE_RATE, _finite

__all__ = ["Beamspace", "Weights"]


@dataclass(frozen=True)
class Beamspace:
    """Angles in degrees, viewed outward from the array; broadside is zero."""

    azimuth_deg: float = 0
    elevation_deg: float = 0
    squint: bool = False

    def coefficients(self, position, frequency, waveform):
        az = _finite(self.azimuth_deg, "azimuth_deg")
        el = _finite(self.elevation_deg, "elevation_deg")
        if abs(az) > 90 or abs(el) > 90:
            raise ValueError("Beamspace angles must lie within ±90 degrees")
        if position is None:
            raise ValueError("Beamspace steering requires antenna geometry; use Pool.from_config()")
        az, el = np.deg2rad([az, el])
        delay = (position[0] * np.sin(az) * np.cos(el) + position[1] * np.sin(el)) / SPEED_OF_LIGHT
        if not self.squint and isinstance(waveform, CW):
            frequency += waveform.offset_hz
        return np.exp(-2j * np.pi * frequency * delay), delay * SAMPLE_RATE if self.squint else 0.0


@dataclass(frozen=True)
class Weights:
    """Per-antenna complex weights and additional delays in seconds.

    Unspecified antennas use unit weight and zero delay.
    """

    values: object
    delays_s: object = None

    def __post_init__(self):
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "delays_s", MappingProxyType(dict(self.delays_s or {})))
        for a, value in self.values.items():
            if not isinstance(a, Antenna) or not np.isfinite(complex(value)):
                raise ValueError("Weights must map Antenna objects to finite complex values")
        for a, value in self.delays_s.items():
            if not isinstance(a, Antenna):
                raise ValueError("Delays must use Antenna keys")
            _finite(value, "delay")
