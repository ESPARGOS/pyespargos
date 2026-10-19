"""Pure phase/delay estimation for reciprocal TX calibration, without device I/O."""

import numpy as np


def _fit_link(frequencies, response, bin_hz):
    """Fit phase and delay using all tones, including sparse, irregular combs."""
    z = response / abs(response)
    bins = np.rint((frequencies - frequencies[0]) / bin_hz).astype(int)
    step = np.gcd.reduce(np.diff(bins))
    # Find the delay within the comb's unambiguous interval.
    # Zero padding gives an eighth-sample estimate without a delay search loop.
    spectrum = np.zeros(8 * round(80e6 / (bin_hz * step)), complex)
    spectrum[bins // step] = z
    delays = np.fft.fftfreq(len(spectrum), d=bin_hz * step / 80e6)
    # RX synchronization already aligns the sample grids. Stay within one
    # eight-way pilot comb interval instead of accepting distant delay aliases.
    candidates = abs(delays) < 80e6 / (16 * bin_hz)
    coarse = 2 * np.pi * delays[candidates][np.argmax(abs(np.fft.fft(spectrum))[candidates])]
    f = frequencies / 80e6
    phase = np.angle(np.sum(z * np.exp(-1j * coarse * f)))
    residual = np.angle(z * np.exp(-1j * (phase + coarse * f)))
    # A narrow interferer can be coherent but is not reciprocal. Do not unwrap
    # through it: one bad tone can introduce a whole 2π step. The coarse delay
    # puts clean residuals near zero, allowing a small, bounded outlier removal.
    keep = abs(residual) < 0.5
    if keep.sum() < max(6, np.ceil(0.9 * len(z))) or np.ptp(frequencies[keep]) < 10e6:
        return phase, coarse / (2 * np.pi), np.inf
    slope, offset = np.polyfit(f[keep], residual[keep], 1)
    phase += offset
    slope += coarse
    rms = np.sqrt(np.mean(np.angle(z[keep] * np.exp(-1j * (phase + slope * f[keep]))) ** 2))
    return phase, slope / (2 * np.pi), rms


def fit_links(frequencies, response, valid):
    # response[f, receiver, transmitter] has the calibrated RX response removed.
    n = response.shape[1]
    if n == 1:
        return np.zeros(1), np.zeros(1), {"links": 0, "phase_residual_deg": 0.0, "delay_residual_samples": 0.0}
    h = np.zeros((n, n), complex)
    edges, slopes, weights = [], [], []
    bin_hz = np.min(np.diff(frequencies))
    for i in range(n):
        for j in range(i):
            good = valid[:, j, i] & valid[:, i, j]
            if good.sum() < 6 or np.ptp(frequencies[good]) < 10e6:
                continue
            phase, delay, rms = _fit_link(frequencies[good], response[good, j, i] / response[good, i, j], bin_hz)
            if rms > 0.2:
                continue
            weight = 1 / max(0.02, rms) ** 2
            h[i, j] = weight * np.exp(1j * phase)
            h[j, i] = h[i, j].conjugate()
            edge = np.zeros(n - 1)
            if i:
                edge[i - 1] = 1
            if j:
                edge[j - 1] = -1
            edges.append(edge)
            slopes.append(delay)
            weights.append(weight)
    if not edges or np.linalg.matrix_rank(edges) < n - 1:
        raise RuntimeError("Reciprocal links do not connect every selected transmitter")
    # Connection Laplacian avoids degree bias when some paths are weak.
    laplacian = np.diag(np.sum(abs(h), axis=1)) - h
    _, v = np.linalg.eigh(laplacian)
    phase = np.angle(v[:, 0] / v[0, 0])
    w = np.sqrt(weights)
    delay = np.r_[0, np.linalg.lstsq(np.asarray(edges) * w[:, None], np.asarray(slopes) * w, rcond=None)[0]]
    residual = [np.angle(h[i, j] * np.exp(-1j * (phase[i] - phase[j]))) for i in range(n) for j in range(i) if h[i, j]]
    return (
        -np.rad2deg(phase),
        delay,
        {
            "links": len(edges),
            "phase_residual_deg": float(np.rad2deg(np.sqrt(np.mean(np.square(residual))))),
            "delay_residual_samples": float(np.sqrt(np.mean((np.asarray(edges) @ delay[1:] - slopes) ** 2))),
        },
    )
