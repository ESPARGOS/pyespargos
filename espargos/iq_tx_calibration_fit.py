"""Controller-monitor pilot model and relative phase/delay estimator."""

import numpy as np


def pilots(n=1024, peak=192, variant=0):
    """Match sensor integer-IFFT synthesis, including integer sample rounding."""
    quarter = np.minimum(32767, np.floor(32767 * np.sin(np.arange(1025) * 2 * np.pi / 4096) + 0.5)).astype(np.int64)

    def sine(p):
        p = p % 4096
        quadrant, x = p // 1024, p % 1024
        return quarter[np.where(quadrant & 1, 1024 - x, x)] * np.where(quadrant >= 2, -1, 1)

    bins = np.array([k for k in range(-n * 3 // 8, n * 3 // 8 + 1) if abs(k) >= 8])
    owner = np.where(abs(bins) == n // 16 + 1, 0, abs(bins) % 8)
    result = []
    for slot in range(8):
        k = bins[owner == slot]
        phase = ((k * k * (37 + 11 * variant) + 190 * k) * 4096 + 1000) // 2000
        z = np.zeros((n, 2), dtype=np.int64)
        z[k % n, 0], z[k % n, 1] = sine(phase + 1024), sine(phase)
        j = 0
        for i in range(1, n):
            bit = n >> 1
            while j & bit:
                j ^= bit
                bit >>= 1
            j ^= bit
            if i < j:
                z[[i, j]] = z[[j, i]]
        length = 2
        while length <= n:
            for b in range(length // 2):
                r, q = sine(b * 4096 // length + 1024), sine(b * 4096 // length)
                x = np.arange(b, n, length)
                y = x + length // 2
                re = (r * z[y, 0] - q * z[y, 1] + 16384) // 32768
                im = (r * z[y, 1] + q * z[y, 0] + 16384) // 32768
                delta = np.column_stack((re, im))
                z[y] = z[x] - delta
                z[x] += delta
            length *= 2
        scale = max(abs(z).max(), 1)
        z = (z * peak + scale // 2) // scale
        z = z[:, 0] + 1j * z[:, 1]
        result.append((k, np.fft.fft(z), z))
    return result


def _phase_fit(k, phase, n):
    nearby = np.diff(k) == 8
    rough = -np.angle(np.exp(1j * np.diff(phase)[nearby]).sum()) * n / (16 * np.pi)
    residual = phase + 2 * np.pi * k * rough / n
    center = np.angle(np.exp(1j * residual).sum())
    residual = np.angle(np.exp(1j * (residual - center)))
    slope, intercept = np.polyfit(k, residual, 1)
    return center + intercept, rough - slope * n / (2 * np.pi), float(np.sqrt(np.mean((residual - slope * k - intercept) ** 2)))


def estimate(samples, models):
    """Fit disjoint combs against the slot-zero reference, as in the web UI."""
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
