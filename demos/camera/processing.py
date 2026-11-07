"""Emission detection and rectangular-array direction finding for IQ demos.

Raw IQ is processed per capture event: a short-time spectrum of every antenna,
a per-bin noise floor that adapts to the receiver (including its spurs), and
one antenna covariance per detected emission. Summing the covariance over the
time-frequency cells of a single emission keeps emitters apart that merely
share a band, which no long-term average can do.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

try:
    from scipy.fft import fft as _fft
except ImportError:
    from numpy.fft import fft as _fft

from espargos.array_processing import root_music_2d, bartlett_fft_2d
from espargos import constants

__all__ = ["Emission", "NoiseFloor", "channelize", "detect_emissions", "direction_grid", "locate", "peak_directions", "flash_image", "spatial_noise_power"]


def channelize(samples, window, reject_dc=True):
    """Windowed spectra of (antennas, segments, samples) IQ blocks.

    DC removal is optional: a carrier exactly at the receiver center is a
    constant complex signal too, so subtracting the mean would erase it.
    """
    samples = np.asarray(samples)
    if reject_dc:
        samples = samples - np.mean(samples, axis=-1, keepdims=True)
    return np.fft.fftshift(_fft(samples * window, axis=-1), axes=-1)


class NoiseFloor:
    """Per-bin noise power, tracked as a low quantile over recent events.

    Intermittent signals occupy a bin only part of the time and therefore do
    not raise the estimate. Spectral and optional spatial noise estimates
    cap the temporal estimate, so persistent emitters remain detectable.
    ``hold_s`` spaces stored spectra; ``antennas`` is the number of receivers
    averaged in the offered power.
    """

    def __init__(self, antennas: int, history: int = 48, quantile: float = 0.25, hold_s: float = 0.25):
        self.history = int(history)
        self.quantile = float(quantile)
        self.hold_s = float(hold_s)
        # The quantile of noise power lies below its mean by a known factor
        self._bias = np.quantile(np.random.default_rng(0).gamma(antennas, 1.0 / antennas, 1 << 14), self.quantile)
        self._spectra = []
        self._last_update = -np.inf
        self.power = None

    def needs_update(self, now):
        """Whether a new spectrum will be used (also gates spatial estimation)."""
        return len(self._spectra) < self.history or now - self._last_update >= self.hold_s

    def update(self, power: np.ndarray, now: float, spatial_noise=None):
        """Offer the ``(segments, bins)`` array-mean power of one event."""

        if not self.needs_update(now):
            return
        self._last_update = now
        self._spectra.append(np.quantile(power, self.quantile, axis=0))
        del self._spectra[: -self.history]
        if len(self._spectra) >= 8 or power.shape[0] >= 8:
            temporal = np.quantile(self._spectra, self.quantile, axis=0) / self._bias
            # A persistent narrowband emitter must not become its own noise floor.
            # Use the quiet part of the sampled band as a conservative ceiling.
            # The spatial estimate supplies the ceiling when an emitter occupies
            # the full band continuously; no WiFi-specific structure is assumed.
            background = np.quantile(temporal, 0.2)
            ceiling = background * 4.0 if spatial_noise is None else 4.0 * np.asarray(spatial_noise)
            ceiling = np.where(np.isfinite(ceiling) & (ceiling > 0), ceiling, temporal)
            self.power = np.minimum(temporal, np.maximum(ceiling, 1e-20))


@dataclass(frozen=True)
class Emission:
    """One contiguous burst within an event."""

    covariance: np.ndarray  # (antennas, antennas), summed over the active cells
    power: float  # mean per-antenna power of the active cells
    bins: np.ndarray  # active-bin mask
    snapshots: int = 1024


def detect_emissions(spectra: np.ndarray, floor: np.ndarray, threshold_db: float, allowed: np.ndarray, min_cells: int = 2, *, power=None) -> list[Emission]:
    """Split one event into emissions.

    ``spectra`` has shape ``(antennas, segments, bins)``. A time-frequency
    cell is active when its array-mean power exceeds the per-bin ``floor`` by
    ``threshold_db``; runs of consecutive segments with active cells form one
    emission each, so two packets within one capture are located separately.
    """

    if power is None:
        power = np.mean(np.abs(spectra) ** 2, axis=0)
    cells = (power > floor * 10.0 ** (threshold_db / 10.0)) & allowed
    busy = np.any(cells, axis=1)
    edges = np.flatnonzero(np.diff(np.concatenate(([False], busy, [False])).astype(np.int8)))
    emissions = []
    for start, stop in zip(edges[::2], edges[1::2]):
        mask = cells[start:stop]
        occupied = np.flatnonzero(np.any(mask, axis=0))
        # Keep simultaneous, frequency-separated emissions independent. Small
        # gaps (including rejected DC bins) belong to the same occupied band.
        groups = np.split(occupied, np.flatnonzero(np.diff(occupied) > 6) + 1)
        for group in groups:
            if not group.size:
                continue
            selected = np.zeros_like(mask)
            selected[:, group] = mask[:, group]
            count = int(np.count_nonzero(selected))
            if count < min_cells:
                continue
            active = spectra[:, start:stop][:, selected]
            emissions.append(Emission(covariance=active @ np.conj(active.T), power=float(np.sum(power[start:stop][selected]) / count), bins=np.any(selected, axis=0), snapshots=count))
    return emissions


@dataclass(frozen=True)
class DirectionGrid:
    """Array geometry and display dimensions; contains no steering-vector scan."""

    rows: int
    columns: int
    frequency_hz: float
    resolution_azimuth: int
    resolution_elevation: int

    @property
    def shape(self):
        return (self.resolution_azimuth, self.resolution_elevation, self.rows * self.columns)


def direction_grid(rows, columns, frequency_hz, resolution_azimuth, resolution_elevation):
    return DirectionGrid(rows, columns, frequency_hz, resolution_azimuth, resolution_elevation)


def locate(covariance, steering, method="MUSIC", *, snapshots=1024, dynamic_range_db=12.0):
    """Locate using algebraic roots or a spatial FFT, then render the result.

    MUSIC pixels are Gaussian markers of estimated roots, not a sampled
    pseudospectrum or a confidence interval. Purity measures plane-wave fit;
    a reflection may fit just as well as a direct arrival.
    """
    g = steering
    u = (2 * np.arange(g.resolution_azimuth) + 1) / g.resolution_azimuth - 1
    v = (2 * np.arange(g.resolution_elevation) + 1) / g.resolution_elevation - 1
    if method == "MUSIC":
        directions, powers, purity = root_music_2d(covariance, g.rows, g.columns, frequency_hz=g.frequency_hz, snapshots=snapshots, dynamic_range_db=dynamic_range_db)
        spectrum = np.zeros(g.shape[:2])
        # Render only after solving directions. Width is a display choice.
        sigma = max(2 / min(g.shape[:2]), 0.025)
        for (x, y), power in zip(directions, powers):
            distance = ((u[:, None] - x) ** 2 + (v[None, :] - y) ** 2) / sigma**2
            np.maximum(spectrum, power * np.exp(-0.5 * distance), out=spectrum)
    else:
        spectrum = bartlett_fft_2d(covariance, g.rows, g.columns, *g.shape[:2], frequency_hz=g.frequency_hz)
        i, j = np.unravel_index(np.argmax(spectrum), spectrum.shape)
        r, c = np.indices((g.rows, g.columns))
        scale = 2 * np.pi * constants.ANTENNA_SEPARATION * g.frequency_hz / constants.SPEED_OF_LIGHT
        a = np.exp(1j * scale * (c.ravel() * u[i] + r.ravel() * v[j]))
        values, vectors = np.linalg.eigh(covariance)
        purity = float(abs(a.conj() @ vectors[:, -1]) ** 2 / len(a) * values[-1] / max(values.sum(), 1e-30))
    spectrum[u[:, None] ** 2 + v[None, :] ** 2 > 1] = 0
    return spectrum / max(float(np.max(spectrum)), 1e-30), purity


def peak_directions(spectrum: np.ndarray, minimum: float, limit: int = 4, separation: float = 0.3) -> list[tuple[float, float]]:
    """Direction cosines ``(u, v)`` of the strongest maxima of a :func:`direction_grid` spectrum.

    Maxima are refined between pixels, must reach ``minimum`` and lie at
    least ``separation`` apart in ``u``: a rectangular array with few rows
    resolves elevation too poorly to tell two emitters above each other apart.
    """

    def refine(values, index):
        offset = 0.0
        if 0 < index < len(values) - 1:
            below, peak, above = values[index - 1 : index + 2]
            curvature = below - 2.0 * peak + above
            if curvature < 0:
                offset = 0.5 * (below - above) / curvature
        return (2.0 * (index + offset) + 1.0) / len(values) - 1.0

    remaining = np.array(spectrum, dtype=np.float64)
    columns = max(1, int(round(separation * remaining.shape[0] / 2.0)))
    directions = []
    while len(directions) < limit:
        i, j = np.unravel_index(np.argmax(remaining), remaining.shape)
        if not remaining[i, j] >= minimum or remaining[i, j] <= 0:
            break
        directions.append((refine(spectrum[:, j], i), refine(spectrum[i, :], j)))
        remaining[max(0, i - columns) : i + columns + 1] = 0.0
    return directions


def flash_image(spectrum, width_degrees=4.0, azimuth_only=False):
    """Compact visual markers at the estimated directions, not an uncertainty map.

    A low-purity spectrum can have a broad pedestal across the whole view.
    The default display marks the estimated peak; the spectrum remains available
    as a separate diagnostic display. Marker width is a presentation setting.
    """
    peaks = peak_directions(spectrum, 0.1, limit=4)
    result = np.zeros_like(spectrum)
    x = (2 * np.arange(spectrum.shape[0]) + 1) / spectrum.shape[0] - 1
    y = (2 * np.arange(spectrum.shape[1]) + 1) / spectrum.shape[1] - 1
    z2 = 1 - x[:, None] ** 2 - y[None, :] ** 2
    for u, v in peaks:
        if azimuth_only:
            azimuth = np.arctan2(u, np.sqrt(max(1 - u * u - v * v, 0)))
            angles = np.arctan2(x[:, None], np.sqrt(np.maximum(z2, 0)))
            distance = ((angles - azimuth) / max(np.radians(width_degrees), 0.01)) ** 2
        else:
            sigma = max(np.sin(np.radians(width_degrees)), 0.01)
            distance = ((x[:, None] - u) ** 2 + (y[None, :] - v) ** 2) / sigma**2
        i = np.argmin(abs(x - u))
        j = np.argmin(abs(y - v))
        flash = np.where((z2 >= 0) & (distance < 9), spectrum[i, j] * np.exp(-0.5 * distance), 0)
        np.maximum(result, flash, out=result)
    return result


def spatial_noise_power(spectra):
    """Per-bin spatial noise estimate, retaining continuous and colored signals.

    Weak array covariance modes estimate noise independently of temporal duty
    cycle. Short captures pool neighboring frequency bins to obtain enough
    observations; long captures estimate each bin separately. This assumes
    fewer independent signal modes than receivers, not any waveform format.
    """
    antennas, segments, bins = spectra.shape
    covariance = np.einsum("atf,btf->fab", spectra, spectra.conj(), optimize=True) / segments
    if segments < 16:
        width = min(bins, max(3, int(np.ceil(24 / segments))))
        left, right = width // 2, width - 1 - width // 2
        padded = np.pad(covariance, ((left, right), (0, 0), (0, 0)), mode="edge")
        sums = np.concatenate([np.zeros_like(padded[:1]), np.cumsum(padded, axis=0)])
        covariance = (sums[width:] - sums[:-width]) / width
    values = np.linalg.eigvalsh(covariance)
    return np.maximum(np.mean(values[:, : max(1, antennas // 2)], axis=1), 1e-20)
