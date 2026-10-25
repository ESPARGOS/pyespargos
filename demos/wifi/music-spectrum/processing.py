"""One-dimensional MUSIC scan along the columns of a rectangular array."""

import numpy as np

from espargos.constants import ANTENNA_SEPARATION, SPEED_OF_LIGHT


def steering_vectors(columns, angles, frequency_hz):
    """Steering vectors for angles in radians, measured from broadside."""
    positions = np.arange(columns) * ANTENNA_SEPARATION
    return np.exp(-2j * np.pi * frequency_hz / SPEED_OF_LIGHT * np.outer(positions, np.sin(angles)))


def music_spectrum_db(csi, steering, source_count=1):
    """Return a peak-normalized spectrum, or None for empty/invalid input.

    CSI has shape (packets, rows, columns, subcarriers). Each row and subcarrier
    supplies a separate snapshot, so a common phase slope across subcarriers
    cannot cancel the signal. Steering uses the channel's center frequency:
    this is a narrowband approximation, not a joint angle/delay estimator.
    """
    columns = csi.shape[2]
    if not 1 <= source_count < columns:
        raise ValueError("Source count must be at least one and smaller than the number of array columns")
    if csi.size == 0 or not np.isfinite(csi).all():
        return None
    snapshots = np.moveaxis(csi, 2, 0).reshape(columns, -1)
    covariance = snapshots @ snapshots.conj().T / snapshots.shape[1]
    if np.trace(covariance).real <= 0:
        return None
    covariance = (covariance + covariance.conj().T) / 2
    _, eigenvectors = np.linalg.eigh(covariance)
    noise = eigenvectors[:, : columns - source_count]
    denominator = np.sum(np.abs(noise.conj().T @ steering) ** 2, axis=0)
    # Normalize steering-vector energy and cap exact noise-subspace nulls.
    denominator /= np.sum(np.abs(steering) ** 2, axis=0)
    spectrum = -10 * np.log10(np.maximum(denominator, 1e-12))
    return spectrum - spectrum.max()
