#!/usr/bin/env python

"""Fixed gain-state phase compensation for ESPARGOS.

Each receiver gain index selects a combination of analog gain elements, as
defined by the gain table. Crossing an element boundary—where a specific gain
element is turned on, turned off, or bypassed—changes phase as well as
amplitude. Sensors using different AGC states therefore acquire deterministic
relative phase errors. The large jumps are common across sensors and are
corrected here with one fixed, hardcoded table.
"""

from __future__ import annotations

import numpy as np

__all__ = ["GAIN_PHASE_SHIFT_BY_GAIN_RAD", "apply"]


# Piecewise-constant physical phase relative to gain 40.
# Determined in a series of measurements.
GAIN_PHASE_SHIFT_BY_GAIN_RAD = (0.2185,) * 11 + (0.4332,) * 21 + (0.0000,) * 9 + (-0.6404,) * 5 + (-0.1263,) * 6 + (-0.4550,) * 25

_PHASE = np.asarray(GAIN_PHASE_SHIFT_BY_GAIN_RAD)
_REFERENCE_GAIN = 40


def apply(values, rx_gain):
    """Apply the phase correction, broadcasting gains over trailing axes."""

    values = np.asarray(values)
    gains = np.asarray(rx_gain, dtype=np.float64)
    if gains.ndim > values.ndim:
        raise ValueError(f"gain shape {gains.shape} is incompatible with data shape {values.shape}")

    finite = np.isfinite(gains)
    rounded = np.rint(np.where(finite, gains, -1))
    valid = finite & (gains == rounded) & (rounded >= 0) & (rounded < len(_PHASE))
    indices = np.where(valid, rounded, _REFERENCE_GAIN).astype(np.int64)
    shape = gains.shape + (1,) * (values.ndim - gains.ndim)

    try:
        valid_for_data = np.broadcast_to(valid.reshape(shape), values.shape)
    except ValueError as error:
        raise ValueError(f"gain shape {gains.shape} cannot be broadcast to data shape {values.shape}") from error
    if np.any(~valid_for_data & np.isfinite(values)):
        raise ValueError("gain metadata is invalid for finite data")

    correction = np.exp(-1j * _PHASE[indices]).astype(np.complex64).reshape(shape)
    return values * correction
