"""Estimate relative TX phase and delay from controller-monitor captures.

The monitor receives several sensors' pilots together. This module separates
those pilots by frequency bin and compares them with the transmitted spectra
modeled by ``pilots``. The monitor calibration workflow uses the resulting
offsets to align transmitters; reciprocal sensor-link fitting is implemented
separately in ``reciprocity_fit``.
"""

import numpy as np


def _phase_fit(k, phase, n):
    nearby = np.diff(k) == 8
    rough = -np.angle(np.exp(1j * np.diff(phase)[nearby]).sum()) * n / (16 * np.pi)
    residual = phase + 2 * np.pi * k * rough / n
    center = np.angle(np.exp(1j * residual).sum())
    residual = np.angle(np.exp(1j * (residual - center)))
    slope, intercept = np.polyfit(k, residual, 1)
    return center + intercept, rough - slope * n / (2 * np.pi), float(np.sqrt(np.mean((residual - slope * k - intercept) ** 2)))


def estimate(samples, models):
    """Estimate relative TX phase and delay from a controller-monitor capture.

    ``samples`` contains the summed received pilots; ``models`` contains their
    transmitted spectra from ``pilots.pilots``, with the slot-zero pilot first. Average
    complete waveform periods, separate the pilots by their frequency bins, and
    divide by the model spectra. The slot-zero pilot resolves the capture's
    waveform origin and supplies a common phase-curvature reference.

    Return phase offsets in radians and delay offsets in samples, both relative
    to the first model. Reject clipped captures and inconsistent pilot fits.
    """
    n = len(models[0][2])
    if len(samples) < n or max(abs(samples.real).max(), abs(samples.imag).max()) > 470:
        raise ValueError("Monitor capture is too short or clips")
    spectrum = np.fft.fft(samples[: len(samples) // n * n].reshape(-1, n).mean(axis=0))
    points = [(k, np.angle(spectrum[k % n] / model[k % n])) for k, model, _ in models]
    k, phase = points[0]
    selected = k % 8 == 0
    _, rough, _ = _phase_fit(k[selected], phase[selected], n)
    candidates = rough + np.arange(-4, 4) * n / 8
    weights = np.where(selected, 1, 8)
    scores = abs((np.exp(1j * (phase[:, None] + 2 * np.pi * k[:, None] * candidates / n)) * weights[:, None]).sum(axis=0)) / weights.sum()
    origin = candidates[np.argmax(scores)]
    if scores.max() < 0.95:
        raise ValueError("Reference pilots do not identify a consistent symbol origin")
    aligned = [(k, phase + 2 * np.pi * k * origin / n) for k, phase in points]
    k, phase = aligned[0]
    ref_phase, ref_delay, _ = _phase_fit(k, phase, n)
    curvature = np.angle(np.exp(1j * (phase - ref_phase + 2 * np.pi * k * ref_delay / n)))
    fits = []
    for bins, phases in aligned:
        common = np.interp(bins, k, curvature)
        # Linear extrapolation at the outermost interlaced bins, matching JS.
        for mask, left, right in [(bins < k[0], 0, 1), (bins > k[-1], -2, -1)]:
            common[mask] = curvature[left] + (curvature[right] - curvature[left]) * (bins[mask] - k[left]) / (k[right] - k[left])
        fits.append(_phase_fit(bins, phases - common, n))
    fits = np.array(fits)
    phase = np.angle(np.exp(1j * (fits[:, 0] - fits[0, 0])))
    delay = fits[:, 1] - fits[0, 1]
    if np.any(fits[:, 2] > 0.3) or np.any(abs(delay) > n * 3 / 64):
        raise ValueError("Inconsistent monitor pilots or coarse timing")
    return phase, delay


def measure(monitor, models, timeout):
    fits, last_error = [], None
    for _ in range(3):
        for samples in monitor.collect(6, timeout):
            try:
                fits.append(estimate(samples, models))
            except ValueError as error:
                last_error = error
        if len(fits) >= 6:
            break
    if len(fits) < 6:
        raise RuntimeError(f"Not enough clean monitor captures: {last_error}")
    phases = np.exp(1j * np.array([f[0] for f in fits])).mean(axis=0)
    if np.any(abs(phases) < 0.99):
        raise RuntimeError("Monitor pilot phase is not stable")
    return np.angle(phases), np.mean([f[1] for f in fits], axis=0)
