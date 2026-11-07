"""Feed-separated phase estimates and presentation shared by measurement demos."""
import numpy as np
from matplotlib import colormaps


def phase_arrays(covariances, counts, feeds, split=False):
    """Render one array, or independently referenced arrays for RF feeds 2 and 3.

    Inputs are packet-by-sensor covariances, overlap counts and effective feeds.
    Only simultaneous observations of the same feed contribute in split mode.
    Missing antennas stay gray. Disconnected groups cannot share a phase gauge.
    """
    covariances, counts, feeds = map(np.asarray, (covariances, counts, feeds))
    n = feeds.shape[-1]
    panels = []
    for route in ((2, 3) if split else (None,)):
        weights = counts.copy()
        if route is not None:
            selected = feeds == route
            weights = weights * (selected[:, :, None] & selected[:, None, :])
        total = np.sum(weights, axis=0)
        covariance = np.divide(np.sum(covariances * weights, axis=0), total,
                               out=np.zeros((n, n), complex), where=total > 0)
        colors = [[.20, .26, .30, 1.]] * n
        labels = ["—"] * n
        usable = (np.diag(total) > 0) & (np.diag(covariance).real > 0)
        observed = np.flatnonzero(usable)
        if len(observed):
            connected = np.zeros(n, bool)
            connected[observed[0]] = True
            for _ in range(n):
                expanded = (connected | np.any((total > 0)[connected], axis=0)) & usable
                if np.array_equal(expanded, connected):
                    break
                connected = expanded
            indices = np.flatnonzero(connected)
            matrix = covariance[np.ix_(indices, indices)]
            _, vectors = np.linalg.eigh((matrix + matrix.conj().T) / 2)
            vector = vectors[:, -1]
            angles = np.angle(vector * np.conj(vector[0]))
            for index, angle in zip(indices, angles):
                colors[index] = list(colormaps["twilight"]((angle + np.pi) / (2*np.pi)))
                labels[index] = f"{angle:+.2f} rad"
        panels.append(dict(colors=colors, labels=labels))
    return panels


def sample_covariances(samples):
    """Pairwise second moments of (packet, sensor, sample) complex waveforms."""
    valid = np.isfinite(samples)
    values = np.where(valid, samples, 0)
    counts = np.einsum("dnf,dmf->dnm", valid.astype(float), valid.astype(float))
    total = np.einsum("dnf,dmf->dnm", values, values.conj())
    return np.divide(total, counts, out=np.zeros_like(total), where=counts > 0), counts
