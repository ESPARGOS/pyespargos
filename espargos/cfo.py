"""Convert manual receiver CFO correction between Hz and the NRXFOE field."""

import math

# Signed 13-bit forced NCO increment, opposite in sign to packet CFO.
# Hz inputs/readback use WiFiPacketRxControlV3.cfo's convention; invert at
# the register boundary so callers can directly apply a measured packet CFO.
CORRECTION_HZ_PER_UNIT = 80_000_000 / (1 << 20)
CORRECTION_MIN_HZ = -4095 * CORRECTION_HZ_PER_UNIT
CORRECTION_MAX_HZ = 4096 * CORRECTION_HZ_PER_UNIT


def correction_hz_to_raw(value_hz: float) -> int:
    """Quantize Hz to the nearest supported correction; reject invalid inputs."""
    value_hz = float(value_hz)
    if not math.isfinite(value_hz) or not CORRECTION_MIN_HZ <= value_hz <= CORRECTION_MAX_HZ:
        raise ValueError(f"CFO must be finite and between {CORRECTION_MIN_HZ} and {CORRECTION_MAX_HZ} Hz")
    # Match JavaScript Math.round, including negative half-step values.
    return math.floor(-value_hz / CORRECTION_HZ_PER_UNIT + 0.5)


def correction_raw_to_hz(value: int) -> float:
    """Convert a signed 13-bit correction to Hz."""
    if not -4096 <= value <= 4095 or int(value) != value:
        raise ValueError("CFO correction must be an integer between -4096 and 4095")
    return -value * CORRECTION_HZ_PER_UNIT
