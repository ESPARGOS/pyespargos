#!/usr/bin/env python

"""Spatial array processing.

Steering vectors and spatial pseudo-spectra (MUSIC, MVDR) computed from a
caller-provided covariance matrix. Independent of any measurement format.
"""

import numpy as np
from . import constants

__all__ = ["estimate_signal_count", "music_spectrum", "mvdr_spectrum", "steering_vectors_2d", "mdl_source_count", "root_music_2d", "bartlett_fft_2d", "spatial_smoothing_2d"]


def steering_vectors_2d(n_rows, n_columns, resolution_azimuth, resolution_elevation, *, frequency_hz=None, pixel_centers=False):
    """Return a rectangular-array steering grid.

    The legacy default spans phase increments -pi..pi. With frequency_hz,
    use the physical element spacing and direction cosines -1..1. Camera
    textures should use pixel_centers=True so sampled directions coincide
    with their texel centers, rather than the outer texture boundaries.
    """
    scale = np.pi if frequency_hz is None else 2 * np.pi * constants.ANTENNA_SEPARATION * frequency_hz / constants.SPEED_OF_LIGHT

    def axis(count):
        return (2 * np.arange(count) + 1) / count - 1 if pixel_centers else np.linspace(-1, 1, count)

    phase_columns = np.outer(np.arange(n_columns), scale * axis(resolution_azimuth))
    phase_rows = np.outer(np.arange(n_rows), scale * axis(resolution_elevation))
    return np.exp(1.0j * (phase_columns[np.newaxis, :, :, np.newaxis] + phase_rows[:, np.newaxis, np.newaxis, :]))


def estimate_signal_count(eigenvalues, *, dynamic_range_db=None):
    """Conservative signal rank from ascending covariance eigenvalues.

    Assumes at least half the spatial modes contain receiver noise. Modes
    must exceed the upper edge of that noise group by 6 dB; the relative
    floor avoids counting numerical residue in noiseless covariances.
    Optional dynamic_range_db excludes weak model-error modes from the signal
    subspace. This cannot separate perfectly coherent paths or a full-rank scene.
    """
    values = np.maximum(np.sort(np.real(eigenvalues)), 0)
    n = len(values)
    ceiling = max(4 * values[max(0, n // 2 - 1)], values[-1] * 1e-5, 1e-30)
    if dynamic_range_db is not None:
        noise = np.median(values[:max(1, n // 2)])
        ceiling = max(ceiling, noise + (values[-1] - noise) * 10 ** (-float(dynamic_range_db) / 10))
    return min(max(1, int(np.count_nonzero(values > ceiling))), max(1, n // 2))


def music_spectrum(covariance, steering_vectors, source_count=1):
    """Compute a MUSIC spectrum for a caller-provided covariance matrix."""

    steering = steering_vectors.reshape(-1, steering_vectors.shape[2], steering_vectors.shape[3])
    covariance = (covariance + np.conj(covariance.T)) / 2
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    if source_count is None:
        source_count = estimate_signal_count(eigenvalues)
    order = np.argsort(eigenvalues)[::-1]
    noise_subspace = eigenvectors[:, order][:, int(source_count) :]
    denominator = np.linalg.norm(np.einsum("ae,a...->e...", noise_subspace, np.conj(steering)), axis=0)
    spectrum = 1 / np.maximum(denominator, 1e-12)
    return spectrum - np.min(spectrum) + 1e-6


def mvdr_spectrum(covariance, steering_vectors, diagonal_loading=0.1):
    """Compute an MVDR spectrum for a caller-provided covariance matrix."""

    steering = steering_vectors.reshape(-1, steering_vectors.shape[2], steering_vectors.shape[3])
    covariance = (covariance + np.conj(covariance.T)) / 2
    loading = float(diagonal_loading) * np.trace(covariance) / covariance.shape[0]
    covariance_inv = np.linalg.pinv(covariance + loading * np.eye(covariance.shape[0]))
    denominator = np.einsum("a...,ab,b...->...", np.conj(steering), covariance_inv, steering)
    spectrum = 1.0 / np.maximum(np.real(denominator), 1e-12)
    return spectrum - np.min(spectrum) + 1e-6


def mdl_source_count(eigenvalues, snapshots):
    """Wax–Kailath/Rissanen MDL for a complex sample covariance.

    ``snapshots`` counts the observations used in the covariance, not the
    display pixels. White spatial noise and independent snapshots are the
    model assumptions; correlated multipath can reduce the observable rank.
    """
    values = np.sort(np.maximum(np.real(eigenvalues), 0))[::-1]
    n = len(values)
    values = np.maximum(values, max(values[0] * 1e-12, 1e-30))
    samples = max(float(snapshots), n + 1)
    k = np.arange(n)
    remaining = n - k
    arithmetic = np.cumsum(values[::-1])[::-1] / remaining
    log_geometric = np.cumsum(np.log(values[::-1]))[::-1] / remaining
    scores = samples * remaining * (np.log(arithmetic) - log_geometric) + 0.5 * k * (2 * n - k) * np.log(samples)
    return int(np.argmin(scores))


def _noise_polynomial(projector):
    """Ascending coefficients of z**(M-1) a(z)^H P a(z)."""
    size = len(projector)
    return np.asarray([np.trace(projector, offset=k) for k in range(1 - size, size)])


def _inside_roots(coefficients):
    coefficients = np.asarray(coefficients)
    tolerance = max(float(np.max(np.abs(coefficients))) * 1e-12, 1e-30)
    nonzero = np.flatnonzero(np.abs(coefficients) > tolerance)
    if len(nonzero) < 2:
        return np.empty(0, complex)
    roots = np.roots(coefficients[nonzero[0] : nonzero[-1] + 1][::-1])
    roots = roots[(np.abs(roots) < 1) & (np.abs(roots) > 1e-10)]
    return roots[np.argsort(1 - np.abs(roots))]


def _polynomial_determinant(matrix):
    """Small polynomial determinant by subset expansion (no angle sampling)."""
    n = len(matrix)
    if n == 2:
        return np.convolve(matrix[0, 0], matrix[1, 1]) - np.convolve(matrix[0, 1], matrix[1, 0])
    terms = {0: np.ones(1, complex)}
    for mask in range(1 << n):
        if mask not in terms or mask == (1 << n) - 1:
            continue
        row = mask.bit_count()
        for col in range(n):
            if mask & (1 << col):
                continue
            term = np.convolve(terms[mask], matrix[row, col])
            if (mask >> (col + 1)).bit_count() % 2:
                term = -term
            target = mask | (1 << col)
            terms[target] = terms[target] + term if target in terms else term
    return terms[(1 << n) - 1]


def root_music_2d(covariance, rows, columns, *, frequency_hz, snapshots=1024, source_count=None, dynamic_range_db=None):
    """Paired direction cosines and source powers from a uniform rectangular array.

    Reduced-dimension root-MUSIC: root the determinant of the polynomial
    noise matrix along the longer axis, then root its nullspace along the
    other axis. No angular grid is evaluated. The smaller array dimension
    determines the polynomial matrix size (2 for a 2x4, 4 for a 6x4 array).

    Returns ``(directions[K,2], powers[K], purity)``. Directions are front-view
    right/down cosines. Purely coherent paths need additional diversity to
    become separately identifiable; two rows cannot resolve two arrivals
    sharing the same horizontal cosine. MDL estimates observable signal rank,
    not a guaranteed number of physical transmitters.

    Optional ``dynamic_range_db`` limits automatic rank to covariance modes
    within this power range of the strongest noise-subtracted eigenvalue.
    It guards against high-SNR model errors being treated as weak sources;
    omit it for the ideal white-noise MDL model. Explicit source_count wins.
    """
    n = rows * columns
    original = np.asarray(covariance).reshape(n, n)
    if rows < 2 or columns < 2:
        raise ValueError("2-D root-MUSIC requires at least two rows and columns")
    transposed = rows > columns
    if transposed:
        order = np.arange(n).reshape(rows, columns).T.ravel()
        covariance = original[np.ix_(order, order)]
        rows, columns = columns, rows
    else:
        covariance = original
    covariance = (covariance + covariance.conj().T) * 0.5
    values, vectors = np.linalg.eigh(covariance)
    count = mdl_source_count(values, snapshots) if source_count is None else int(source_count)
    if source_count is None and dynamic_range_db is not None:
        # MDL assumes an exact manifold and white receiver noise. At high SNR,
        # small calibration/model errors otherwise become extra "sources".
        # Do not spend subspace dimensions on modes below the requested useful
        # power range. This is a declared resolution limit, not an SNR estimate.
        noise_floor = max(float(np.median(values[:max(1, n // 2)])), 0.0)
        threshold = noise_floor + max(values[-1] - noise_floor, 0) * 10 ** (-float(dynamic_range_db) / 10)
        count = min(count, int(np.count_nonzero(values > threshold)))
    count = min(max(count, 0), n - rows)
    empty = (np.empty((0, 2)), np.empty(0), 0.0)
    if count == 0:
        return empty
    noise = vectors[:, : n - count]
    projector = (noise @ noise.conj().T).reshape(rows, columns, rows, columns)
    coefficients = np.zeros((rows, rows, 2 * columns - 1), complex)
    for c in range(columns):
        for d in range(columns):
            coefficients[:, :, d - c + columns - 1] += projector[:, c, :, d]
    roots = _inside_roots(_polynomial_determinant(coefficients))
    scale = 2 * np.pi * constants.ANTENNA_SEPARATION * frequency_hz / constants.SPEED_OF_LIGHT
    roots = roots[np.abs(np.angle(roots)) <= scale + 1e-7][:count]
    groups = []
    for root in roots:
        phase = np.angle(root)
        for group in groups:
            if abs(np.angle(np.exp(1j * (phase - group[0])))) < 0.005:
                group[1] += 1
                break
        else:
            groups.append([phase, 1])
    directions = []
    for phase, multiplicity in groups:
        a = np.exp(1j * phase * np.arange(columns))
        conditional = np.einsum("c,rcsd,d->rs", a.conj(), projector, a)
        if rows == 2:
            # The quadratic root's phase has a closed form for two sensors.
            # Avoid a second eigendecomposition and polynomial solve per root.
            vertical_roots = [np.exp(1j * np.angle(-conditional[0, 1].conjugate()))]
        else:
            _, basis = np.linalg.eigh(conditional)
            rank = min(multiplicity, rows - 1)
            conditional_noise = basis[:, rank:] @ basis[:, rank:].conj().T
            vertical_roots = _inside_roots(_noise_polynomial(conditional_noise))[:rank]
        for root in vertical_roots:
            u, v = phase / scale, np.angle(root) / scale
            if u * u + v * v > 1 + 1e-7:
                continue
            directions.append((v, u) if transposed else (u, v))
    if not directions:
        return empty
    directions = np.asarray(directions)
    original_rows, original_columns = (columns, rows) if transposed else (rows, columns)
    r, c = np.indices((original_rows, original_columns))
    manifold = np.exp(1j * scale * (c.ravel()[:, None] * directions[:, 0] + r.ravel()[:, None] * directions[:, 1]))
    # Fit nonnegative covariance contributions, not inverse-manifold amplitudes.
    # Nearly coincident roots made the latter explode and render a weak/spurious
    # pair brighter than the measured signal itself.
    gram = np.abs(manifold.conj().T @ manifold) ** 2
    rhs = np.real(np.einsum("ai,ab,bi->i", manifold.conj(), original, manifold))
    powers = np.maximum(np.linalg.lstsq(gram, rhs, rcond=1e-6)[0], 0)
    for _ in range(32):
        previous = powers.copy()
        for i in range(len(powers)):
            powers[i] = max(0.0, powers[i] + (rhs[i] - gram[i] @ powers) / gram[i, i])
        if np.max(np.abs(powers - previous)) <= 1e-6 * max(np.max(powers), 1e-30):
            break
    # Fit quality is evaluated only at algebraically estimated directions.
    principal = vectors[:, -1]
    if transposed:
        principal = principal.reshape(rows, columns).T.ravel()
    match = np.abs(manifold.conj().T @ principal) ** 2 / n
    purity = float(np.max(match) * max(values[-1], 0) / max(np.trace(original).real, 1e-30))
    order = np.argsort(powers)[::-1]
    return directions[order], powers[order], purity


def bartlett_fft_2d(covariance, rows, columns, resolution_azimuth, resolution_elevation, *, frequency_hz):
    """Bartlett power via one 2-D FFT of the summed spatial covariance lags.

    Equivalent to averaging squared 2-D FFTs of the antenna snapshots, without
    FFTing every snapshot separately. Interpolate FFT phase bins onto physical
    direction cosines using the actual element spacing in wavelengths.
    """
    r, c = np.indices((rows, columns))
    r, c = r.ravel(), c.ravel()
    # Twice the display grid reduces interpolation error, not angular resolution.
    na, ne = 2 * resolution_azimuth, 2 * resolution_elevation
    indices = ((r[:, None] - r[None, :]) % ne) * na + (c[:, None] - c[None, :]) % na
    covariance = np.asarray(covariance).ravel()
    lags = np.bincount(indices.ravel(), weights=covariance.real, minlength=ne * na) + 1j * np.bincount(indices.ravel(), weights=covariance.imag, minlength=ne * na)
    power = np.fft.fft2(lags.reshape(ne, na)).real
    scale = constants.ANTENNA_SEPARATION * frequency_hz / constants.SPEED_OF_LIGHT
    u = (2 * np.arange(resolution_azimuth) + 1) / resolution_azimuth - 1
    v = (2 * np.arange(resolution_elevation) + 1) / resolution_elevation - 1
    x, y = (u * scale * na) % na, (v * scale * ne) % ne
    x0, y0 = x.astype(int), y.astype(int)
    wx, wy = x - x0, y - y0
    along_x = power[:, x0] * (1 - wx) + power[:, (x0 + 1) % na] * wx
    result = (along_x[y0] * (1 - wy[:, None]) + along_x[(y0 + 1) % ne] * wy[:, None]).T
    return np.where(u[:, None] ** 2 + v[None, :] ** 2 <= 1, np.maximum(result, 0), 0)


def spatial_smoothing_2d(covariance, rows, columns):
    """Forward/backward smoothed covariance of overlapping rectangular subarrays.

    Shorten the longer dimension by one element when possible. This sacrifices
    some angular resolution to restore rank for coherent multipath. A 2x4 array
    produces a 2x3 covariance. Returns covariance and its new row/column counts.
    """
    sub_rows = rows - 1 if rows > columns and rows > 2 else rows
    sub_columns = columns - 1 if columns >= rows and columns > 2 else columns
    indices = np.arange(rows * columns).reshape(rows, columns)
    blocks = []
    for r in range(rows - sub_rows + 1):
        for c in range(columns - sub_columns + 1):
            selection = indices[r:r + sub_rows, c:c + sub_columns].ravel()
            blocks.append(np.asarray(covariance)[np.ix_(selection, selection)])
    result = np.mean(blocks, axis=0)
    result = .5 * (result + result[::-1, ::-1].conj())
    return result, sub_rows, sub_columns
