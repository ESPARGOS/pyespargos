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


class DisconnectedLinks(RuntimeError):
    """Accepted reciprocal measurements do not connect the selected sensors."""

    def __init__(self, components):
        super().__init__("Reciprocal links do not connect every selected transmitter")
        self.components = components


def _links(frequencies, response, valid):
    n = response.shape[1]
    links = []
    neighbors = [set() for _ in range(n)]
    bin_hz = np.min(np.diff(frequencies))
    for i in range(n):
        for j in range(i):
            good = valid[:, j, i] & valid[:, i, j]
            good &= np.isfinite(response[:, j, i]) & np.isfinite(response[:, i, j])
            good &= (abs(response[:, j, i]) > 0) & (abs(response[:, i, j]) > 0)
            if good.sum() < 6 or np.ptp(frequencies[good]) < 10e6:
                continue
            phase, delay, rms = _fit_link(frequencies[good], response[good, j, i] / response[good, i, j], bin_hz)
            if not np.isfinite(rms) or rms > 0.2:
                continue
            links.append((i, j, phase, delay, 1 / max(0.02, rms) ** 2))
            neighbors[i].add(j)
            neighbors[j].add(i)
    remaining = set(range(n))
    components = []
    while remaining:
        component, pending = set(), {min(remaining)}
        while pending:
            node = pending.pop()
            component.add(node)
            pending.update(neighbors[node] - component)
        remaining -= component
        components.append(sorted(component))
    if len(components) > 1:
        raise DisconnectedLinks(components)
    return links


def _residuals(links, phase, delay):
    # phase is in radians and delay in samples, in the measured-link convention.
    p = [np.angle(np.exp(1j * (value - phase[i] + phase[j]))) for i, j, value, _, _ in links]
    d = [value - delay[i] + delay[j] for i, j, _, value, _ in links]
    return {
        "links": len(links),
        "phase_residual_deg": float(np.rad2deg(np.sqrt(np.mean(np.square(p))))) if p else 0.0,
        "delay_residual_samples": float(np.sqrt(np.mean(np.square(d)))) if d else 0.0,
    }


def fit_links(frequencies, response, valid):
    """Fit relative TX corrections from RX-corrected reciprocal measurements."""
    n = response.shape[1]
    links = _links(frequencies, response, valid)
    h = np.zeros((n, n), complex)
    edges, slopes, weights = [], [], []
    for i, j, phase, delay, weight in links:
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
    if n == 1:
        return np.zeros(1), np.zeros(1), _residuals(links, [0], [0])
    # Connection Laplacian avoids degree bias when some paths are weak.
    laplacian = np.diag(np.sum(abs(h), axis=1)) - h
    _, v = np.linalg.eigh(laplacian)
    phase = np.angle(v[:, 0] / v[0, 0])
    w = np.sqrt(weights)
    delay = np.r_[0, np.linalg.lstsq(np.asarray(edges) * w[:, None], np.asarray(slopes) * w, rcond=None)[0]]
    return -np.rad2deg(phase), delay, _residuals(links, phase, delay)


def validate_links(frequencies, response, valid, phase_deg, delay_samples):
    """Check fixed corrections against separate captures, without refitting."""
    return _residuals(_links(frequencies, response, valid), -np.deg2rad(phase_deg), delay_samples)


def additional_reference(components, measured, valid):
    """Pick a new dense pilot with the most observed links across components."""
    labels = np.zeros(valid.shape[1], int)
    for label, component in enumerate(components):
        labels[component] = label
    candidates = [i for i in range(len(labels)) if i not in measured]
    if not candidates:
        return None

    # Other sensors have already transmitted to this receiver. A dense return
    # pilot can complete these links regardless of their assigned pilot combs.
    def score(i):
        counts = valid[:, i, :].sum(axis=0)
        outside = labels != labels[i]
        return (np.count_nonzero((counts >= 6) & outside), counts[outside].sum(), -i)

    return max(candidates, key=score)
