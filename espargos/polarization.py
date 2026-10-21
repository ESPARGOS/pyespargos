"""ESPARGOS One antenna response and complex polarization estimation."""

import numpy as np


def dominant_jones_vector(vectors: np.ndarray) -> np.ndarray:
    """Return the dominant polarization of complex vectors with shape (..., 2).

    Average coherency matrices, rather than complex fields, so propagation
    phase differences between antennas cannot cancel a common polarization.
    The returned amplitude is the RMS amplitude of the dominant component;
    its largest component is made real and nonnegative to fix common phase.
    For mixed polarizations this represents only the dominant component.
    """
    vectors = np.asarray(vectors, dtype=np.complex128)
    if vectors.ndim < 1 or vectors.shape[-1] != 2 or vectors.size == 0:
        raise ValueError("Expected a nonempty array of Jones vectors with shape (..., 2)")
    points = vectors.reshape(-1, 2)
    coherency = np.einsum("ip,iq->pq", points, points.conj()) / len(points)
    eigenvalues, eigenvectors = np.linalg.eigh(coherency)
    estimate = eigenvectors[:, -1] * np.sqrt(max(float(eigenvalues[-1]), 0.0))
    estimate *= np.exp(-1j * np.angle(estimate[np.argmax(np.abs(estimate))]))
    return estimate


def antenna_response(frequencies):
    """Forward R/L receive matrices for ESPARGOS One (June 2026 revision).

    Interpolate complex H/V-to-R/L response samples over 2.400–2.484 GHz.
    The same common gain/phase convention is used for both rows. Frequencies
    are in Hz; output shape is ``(*frequencies.shape, 2, 2)``. Extrapolation
    is rejected because this revision model covers only the 2.4 GHz band.
    """
    from ._antenna_response import FREQUENCIES, RESPONSE

    frequencies = np.asarray(frequencies, dtype=float)
    if not np.all(np.isfinite(frequencies)) or np.any(frequencies < FREQUENCIES[0]) or np.any(frequencies > FREQUENCIES[-1]):
        raise ValueError("ESPARGOS One antenna response requires frequencies within 2.400–2.484 GHz")
    result = np.empty((*frequencies.shape, 2, 2), complex)
    for output in range(2):
        for source in range(2):
            samples = RESPONSE[:, output, source]
            result[..., output, source] = np.interp(frequencies, FREQUENCIES, samples.real) + 1j * np.interp(frequencies, FREQUENCIES, samples.imag)
    return result


def inverse_jones_response(orientations, frequencies):
    """Return (rows, columns, subcarriers, H/V, R/L) inverse response matrices."""
    orientations = np.asarray(orientations)
    matrices = antenna_response(frequencies)
    if orientations.ndim != 2 or matrices.ndim != 3 or matrices.shape[-2:] != (2, 2):
        raise ValueError("Expected 2D antenna orientations and a 1D subcarrier frequency grid")
    rotations = np.stack([orientation.rotation_matrix() for orientation in orientations.flat]).reshape(*orientations.shape, 2, 2)
    return np.einsum("mnpo,sof->mnspf", rotations, np.linalg.inv(matrices))
