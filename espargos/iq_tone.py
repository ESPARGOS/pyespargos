#!/usr/bin/env python

"""Reference CW tone planning: frequency reach, high-band placement, sweeps.

The controller's reference tone generator has two regimes:

- from 2310 to 2497 MHz the exact tone API places the tone with kHz
  resolution (``set_reftx_tone``);
- above that, a forced-VCO-cap path reaches ~2680 MHz (``set_reftx_tone_freq``
  with an explicit ``cap``): the VCO free-runs, its frequency set by the coarse
  cap in ~4.7 MHz steps, reproducible to ~1 MHz within the accurate table.

This module holds the measured cap->frequency tables and the pure planning
functions; the RPC keying lives in :class:`espargos.iq_pool.IQPool`.
"""

import numpy as np

__all__ = [
    "HIGHBAND_CAP_FREQ_EDGE_MHZ",
    "HIGHBAND_CAP_FREQ_MHZ",
    "HIGHBAND_TONE_CBW",
    "HIGHBAND_TONE_CODE_HZ",
    "TONE_MAX_HZ",
    "TONE_MIN_HZ",
    "highband_cap_for_freq",
    "tone_sweep_steps",
]

# Exact-tone range of the controller ESP32 RFPLL, verified on hardware
# 2026-09-30. Below 2412 MHz the controller tunes the PLL directly, avoiding
# the WiFi channel-offset overflow that previously limited the lower range.
TONE_MIN_HZ = 2310e6
TONE_MAX_HZ = 2497e6

# High-band reference tone: forced-VCO-cap -> measured tone frequency (MHz).
# Characterized on the reference controller 2026-07-07 (2-pass warm sweep, cbw=4,
# offset 0, sensor-tracked). The map is smooth/monotonic and reproducible to
# ~1 MHz; ~4.7 MHz per cap step (so placement is accurate to ~±2.4 MHz). cbw and
# divider code do NOT affect the forced-cap frequency, and the fine offset is not
# a usable trim under a forced cap, so the cap is the only knob. cap<47
# (>~2634 MHz) is the topmost VCO cap band and its frequency wanders several MHz
# with temperature (caps 43-46 measured 7-16 MHz run-to-run), so it's excluded —
# the reliable ceiling is cap 47 / ~2634 MHz. Board process variation adds a
# fixed sub-MHz-to-few-MHz shift (acceptable; read exact off the waterfall).
HIGHBAND_CAP_FREQ_MHZ = {
    47: 2634.4,
    48: 2633.8,
    49: 2627.5,
    50: 2622.7,
    51: 2616.4,
    52: 2612.3,
    53: 2606.2,
    54: 2601.5,
    55: 2595.4,
    56: 2596.5,
    57: 2590.5,
    58: 2585.8,
    59: 2579.9,
    60: 2576.0,
    61: 2570.1,
    62: 2565.6,
    63: 2559.7,
    64: 2559.2,
    65: 2553.4,
    66: 2549.0,
    67: 2543.3,
    68: 2539.5,
    69: 2533.9,
    70: 2529.5,
    71: 2523.9,
    72: 2525.0,
    73: 2519.4,
    74: 2515.2,
    75: 2509.7,
    76: 2506.1,
    77: 2500.7,
    78: 2496.5,
}
# The topmost VCO cap band (caps 39..46, nominally ~2645..2680 MHz): produces
# equally clean tones (55+ dB SNR) but its landing frequency DRIFTS strongly
# with temperature — ±10 MHz typical, up to +30 MHz at the highest caps on a
# hot board (measured; it walks upward while transmitting). The cap->frequency
# ORDER stays monotonic, so nudging the target still moves the tone the right
# way. Exposed as an explicitly APPROXIMATE zone — the calibration sweeps it
# blindly (it measures where tones land), and the manual tone control places
# here with a "read the exact frequency off the waterfall" caveat.
HIGHBAND_CAP_FREQ_EDGE_MHZ = {
    39: 2679.0,
    40: 2682.0,
    41: 2678.0,
    42: 2673.0,
    43: 2661.0,
    44: 2656.0,
    45: 2649.0,
    46: 2643.0,
}
# Divider code/cbw the tables were measured with; kept fixed so they stay valid.
HIGHBAND_TONE_CODE_HZ = 2630e6
HIGHBAND_TONE_CBW = 4


def highband_cap_for_freq(freq_hz):
    """Best VCO cap for a target high-band frequency, from the hardcoded
    measured tables. Returns (cap, expected_freq_MHz, approximate) —
    approximate=True above the accurate table's ceiling, where the topmost
    VCO cap band wanders +-5..8 MHz thermally."""
    target = freq_hz / 1e6
    if target <= max(HIGHBAND_CAP_FREQ_MHZ.values()):
        cap = min(HIGHBAND_CAP_FREQ_MHZ, key=lambda c: abs(HIGHBAND_CAP_FREQ_MHZ[c] - target))
        return cap, HIGHBAND_CAP_FREQ_MHZ[cap], False
    cap = min(HIGHBAND_CAP_FREQ_EDGE_MHZ, key=lambda c: abs(HIGHBAND_CAP_FREQ_EDGE_MHZ[c] - target))
    return cap, HIGHBAND_CAP_FREQ_EDGE_MHZ[cap], True


def tone_sweep_steps(center_hz, fs, spacing_hz=4e6):
    """Sweep plan hopping the reference tone across the captured band
    [center - 0.45 fs, center + 0.45 fs]: the low band (<= 2497 MHz) uses the
    exact tones (kHz-resolution placement, so narrow bands at low
    sample rates still get distinct tones), above that the forced-VCO-cap
    tones — mixed automatically when the band straddles 2497. Returns a list
    of steps for :meth:`espargos.iq_pool.IQPool.apply_tone_step`:
    ("freq", f_khz) or ("cap", cap)."""
    # The calibration fit searches delays in [-16, 16) samples. Uniform
    # tones repeat the same phase signature every fs / spacing samples;
    # keep that period above the full search width, including grid rounding.
    # A single board can also have offsets near the search limits.
    spacing_hz = min(spacing_hz, fs / 40)
    lo, hi = center_hz - 0.45 * fs, center_hz + 0.45 * fs
    steps = []
    f_lo, f_hi = max(lo, TONE_MIN_HZ), min(hi, TONE_MAX_HZ)
    if f_hi > f_lo:
        margin = min(1e6, 0.05 * fs)
        n_low = max(4, int(round((f_hi - f_lo) / spacing_hz)))
        steps += [("freq", int(round(f / 1e3))) for f in np.linspace(f_lo + margin, f_hi - margin, n_low)]
    steps += [("cap", cap) for cap, fmhz in HIGHBAND_CAP_FREQ_MHZ.items() if lo <= fmhz * 1e6 <= hi]
    if hi > max(HIGHBAND_CAP_FREQ_MHZ.values()) * 1e6:
        # the topmost VCO cap band (caps 39..46) wanders thermally, but for
        # calibration ANY clean tone illuminates bins — where it lands is
        # measured implicitly by the per-bin covariance
        steps += [("cap", cap) for cap in range(39, 47)]
    return steps
