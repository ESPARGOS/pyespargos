"""Packet waveform alignment for qualified BLE captures.

This follows the firmware web demo's native-tap filtering and phase convention.
Packets with unknown CRC seeds require stricter waveform coherence.
"""

from dataclasses import dataclass
from functools import lru_cache
import numpy as np


@dataclass
class Waveform:
    samples: np.ndarray
    begin: int
    end: int
    sps: float
    if_hz: int


@lru_cache(maxsize=16)
def _filter(rate, phy):
    sps = rate / 1e6
    x = np.arange(8 * int(sps) + 1) - 4 * sps
    cutoff = (1.2 if phy == 1 else 0.7) / sps
    taps = 2 * cutoff * np.sinc(2 * cutoff * x) * np.hamming(len(x))
    return taps / taps.sum()


def waveform(packet, phy=None, payload_length=None, coding=None):
    phy = packet.phy if phy is None else phy
    rate = packet.sample_rate
    step = max(1, rate // (8_000_000 if phy == 1 else 4_000_000))
    taps = _filter(rate, phy)
    mixed = packet.samples * np.exp(-2j * np.pi * packet.if_hz / rate * np.arange(len(packet.samples)))
    z = np.convolve(mixed, taps)[: len(mixed) : step]
    sps = rate / step / 1e6
    width = int(8 * sps)
    power = np.convolve(abs(z) ** 2, np.ones(width) / width)[: len(z)]
    minimum = int(np.ceil(len(taps) / step))
    peak = np.max(power[max(minimum, len(z) - int(128 * sps)) :], initial=0)
    end = len(z) - 3
    while end > minimum and power[end] < peak * 0.25:
        end -= 1
    # Account for the power-envelope integration tail. Keep the entire usable
    # packet interval rather than a fixed 96 us suffix. Long packets may be
    # truncated by the firmware history buffer, so never invent an earlier start.
    end -= int(8 * sps)
    if payload_length is None and packet.qualified and len(packet.prefix) >= 2:
        payload_length = packet.prefix[1]
    duration_us = 8 * (payload_length + (11 if phy == 1 else 10)) / (2 if phy == 1 else 1) if payload_length is not None else None
    coding = getattr(packet, "coding", 0) if coding is None else coding
    if payload_length is not None and coding:
        duration_us = 376 + ((payload_length + 5) * 8 + 3) * coding
    begin = max(minimum, end - int(duration_us * sps) + minimum) if duration_us is not None else minimum
    if peak <= 0 or end - begin < 48 * sps:
        begin = end = 0
    return Waveform(z, begin, end, sps, packet.if_hz)


_INTERPOLATION_OFFSETS = np.arange(-7, 9)
_INTERPOLATION_WINDOW = np.kaiser(16, 6)


def _interpolate(z, t):
    """Band-limited fractional delay for the filtered BLE waveform.

    Cubic interpolation biases fractional timing depending on the sample phase.
    At a 2 MHz IF even a small delay bias becomes a visible spatial phase error.
    All alignment windows are contiguous, so one short FIR evaluates the delay.
    """
    n = np.floor(t).astype(int)
    fraction = float(t[0] - n[0])
    offsets = _INTERPOLATION_OFFSETS
    weights = np.sinc(offsets - fraction) * _INTERPOLATION_WINDOW
    weights /= weights.sum()
    if np.all(np.diff(n) == 1):
        return np.correlate(z[n[0]-7:n[-1]+9], weights, mode="valid")
    return np.sum(z[n[:, None] + offsets] * weights, axis=1)


def align_waveforms(reference, other, recovery=False):
    if reference.end <= reference.begin or other.end <= other.begin or reference.sps != other.sps or reference.if_hz != other.if_hz:
        return None
    coarse = other.end - reference.end
    span = 12 * reference.sps
    # Use a common interior for the search. The final measurement can then use
    # the full overlap, including edges omitted only to make the search valid.
    begin = max(reference.begin, int(np.ceil(8 - coarse + span)))
    end = min(reference.end, int(np.floor(len(other.samples) - 9 - coarse - span)))
    if end - begin < 48 * reference.sps:
        return None
    indices = np.arange(begin, end)
    x = reference.samples[indices]
    x_power = np.vdot(x, x).real

    def fit(delay, section=slice(None)):
        t = indices[section] + delay
        if t[0] < 8 or t[-1] >= len(other.samples) - 9:
            return 0.0, 0.0
        y = _interpolate(other.samples, t)
        a = x[section]
        product = np.vdot(a, y)
        norm = np.sqrt((x_power if section == slice(None) else np.vdot(a, a).real) * np.vdot(y, y).real)
        return abs(product) / max(norm, 1e-30), np.angle(product)

    delays = np.arange(coarse - span, coarse + span + 0.01, reference.sps / 4)
    if np.all(delays == np.rint(delays)):
        # Integer coarse offsets need no interpolation. Correlate the whole
        # search window in NumPy, retaining exactly the same fit scores.
        valid = (indices[0] + delays >= 8) & (indices[-1] + delays < len(other.samples) - 9)
        scores = np.zeros(len(delays))
        if np.any(valid):
            offsets = delays[valid].astype(int)
            lo, hi = offsets[0], offsets[-1]
            segment = other.samples[indices[0] + lo : indices[-1] + hi + 1]
            products = np.correlate(segment, x, mode="valid")
            powers = np.convolve(abs(segment) ** 2, np.ones(len(x)), mode="valid")
            scores[valid] = abs(products[offsets - lo]) / np.sqrt(np.maximum(x_power * powers[offsets - lo], 1e-60))
    else:
        scores = np.asarray([fit(d)[0] for d in delays])
    best = delays[np.argmax(scores)]
    lo, hi = best - 1.0, best + 1.0
    ratio = (np.sqrt(5)-1)/2
    a, b = hi-ratio*(hi-lo), lo+ratio*(hi-lo)
    score_a, score_b = fit(a)[0], fit(b)[0]
    for _ in range(20):
        if score_a > score_b:
            hi, b, score_b = b, a, score_a
            a = hi-ratio*(hi-lo)
            score_a = fit(a)[0]
        else:
            lo, a, score_a = a, b, score_b
            b = lo+ratio*(hi-lo)
            score_b = fit(b)[0]
    delay = (lo + hi) / 2
    coherence, phase = fit(delay)
    if coherence < (0.9 if recovery else 0.85):
        return None
    if recovery:
        rivals = scores[abs(delays - delay) >= 2 * reference.sps]
        if coherence - np.max(rivals, initial=0) < 0.06 or abs(delay - coarse) >= span - 0.25 * reference.sps:
            return None
        for part in range(4):
            score, angle = fit(delay, slice(len(x) * part // 4, len(x) * (part + 1) // 4))
            if score < 0.8 or abs(np.angle(np.exp(1j * (angle - phase)))) > 0.25:
                return None
    # Restore the phase introduced by aligning captures mixed at different origins.
    phase += 2 * np.pi * reference.if_hz / (reference.sps * 1e6) * delay
    return WaveformAlignment(float(delay), float(np.angle(np.exp(1j * phase))), float(coherence))


@dataclass(frozen=True)
class WaveformAlignment:
    delay: float
    phase: float
    coherence: float


def relative(reference, other, recovery=False):
    """Relative native-tap phase after compensating the digital IF origin."""
    fit = align_waveforms(reference, other, recovery)
    return None if fit is None else (fit.phase, fit.coherence)


@dataclass(frozen=True)
class BLEPhase:
    """Relative phasors and fit coherence in ``(board, row, column)`` order.

    ``valid`` is independent of the cluster's capture-completion mask. A received
    packet can fail waveform alignment. Invalid positions contain NaN. Vectors
    use the conventional complex receive-phase sign, conjugate to native taps.
    ``amplitude`` retains RMS waveform magnitudes referred to gain index zero
    for polarization reconstruction when measured gain is available. It is separate from the unit phasors and fit coherence.
    """

    vector: np.ndarray
    coherence: np.ndarray
    valid: np.ndarray
    amplitude: np.ndarray | None = None


def estimate_phase(cluster):
    """Estimate uncalibrated narrowband phase from a BLECluster.

    Raw clusters support all firmware PHYs. This estimator currently supports
    LE1M and LE2M only. It neither changes hardware nor modifies the cluster.
    At least one qualified capture with known gain supplies the waveform anchor.
    """
    from .ble_iq import align_packet_iq
    return align_packet_iq(cluster).phase()
