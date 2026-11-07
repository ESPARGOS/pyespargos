"""Short, bounded histories of calibrated BLE phase observations."""

from collections import deque
from threading import Lock
import time
import numpy as np


class BLEPhaseHistory:
    """Covariance history for one source, independent of discovery and rendering.

    Append calibrated array-shaped phasors and their effective RF inputs.
    Optional full-packet covariances and sample counts retain waveform energy
    instead of approximating each packet by a single outer product.
    Only pairs observed on the same antenna polarization contribute. This also
    supports random polarization without mixing phases across orthogonal feeds.
    Both packet count and observation age bound the history.
    """

    def __init__(self, size=16, max_age=0.25, *, clock=None):
        self._clock = clock if clock is not None else lambda: time.monotonic()
        if isinstance(size, bool) or not isinstance(size, int) or size < 1 or not np.isfinite(max_age) or max_age <= 0:
            raise ValueError("BLE phase history size and maximum age must be positive")
        self.size = size
        self.max_age = float(max_age)
        self._observations = deque(maxlen=size)
        self._lock = Lock()
        self._shape = None
        self._last_timestamp = None

    def clear(self):
        with self._lock:
            self._observations.clear()
            self._last_timestamp = None

    def append(self, vector, rf_switch, *, timestamp=None, covariance=None, sample_counts=None):
        """Append an observation using host monotonic time, returning acceptance.

        Arrays are copied. Out-of-order/nonfinite timestamps and inconsistent
        sensor shapes are rejected. An observation with no finite antenna
        measurements is ignored rather than seeding a polarization estimate.
        """
        vector, inputs = np.asarray(vector), np.asarray(rf_switch)
        if vector.ndim == 0 or vector.size == 0 or vector.shape != inputs.shape:
            raise ValueError("Phase and RF input arrays must have the same nonempty shape")
        finite = np.isfinite(vector)
        if np.any(finite & ~np.isin(inputs, (0, 1, 2, 3))):
            raise ValueError("RF inputs must describe effective routes, not Random mode")
        if covariance is None:
            values = np.where(finite, vector, 0).ravel()
            covariance = np.outer(values, values.conj())
            sample_counts = np.outer(finite.ravel(), finite.ravel()).astype(int)
        covariance, sample_counts = np.asarray(covariance), np.asarray(sample_counts)
        if covariance.shape != (vector.size, vector.size) or sample_counts.shape != covariance.shape:
            raise ValueError("Covariance and sample counts must match the sensor count")
        if not np.all(np.isfinite(covariance)) or not np.all(np.isfinite(sample_counts)) or np.any(sample_counts < 0):
            raise ValueError("Covariance and nonnegative sample counts must be finite")
        timestamp = self._clock() if timestamp is None else float(timestamp)
        if not np.isfinite(timestamp):
            raise ValueError("Observation timestamp must be finite")
        with self._lock:
            if self._shape is not None and vector.shape != self._shape:
                raise ValueError("A BLE phase history must use a consistent sensor shape")
            if self._last_timestamp is not None and timestamp < self._last_timestamp:
                raise ValueError("Observation timestamps must be nondecreasing")
            self._expire(self._clock())
            if not np.any(finite & np.isin(inputs, (2, 3))):
                return False
            self._shape = vector.shape
            self._last_timestamp = timestamp
            self._observations.append((timestamp, vector.ravel().copy(), inputs.ravel().copy(), covariance.copy(), sample_counts.copy()))
            self._expire(self._clock())
            return True

    def _expire(self, now):
        while self._observations and now - self._observations[0][0] >= self.max_age:
            self._observations.popleft()

    def _snapshot(self):
        with self._lock:
            self._expire(self._clock())
            return tuple(self._observations)

    @property
    def snapshot_count(self):
        return len(self._snapshot())

    def covariance(self, *, require_complete=True):
        """Return the spatial covariance, or None until all pairs are observed.

        Missing pairs are never filled with invented phase. For randomized feeds,
        pairwise averaging can have small negative eigenvalues. Projecting onto
        the positive-semidefinite cone gives subspace methods a valid covariance.
        """
        observations = self._snapshot()
        if not observations:
            return None
        n = observations[0][1].size
        total, counts = np.zeros((n, n), complex), np.zeros((n, n), float)
        for _, vector, inputs, covariance, sample_counts in observations:
            for route in (2, 3):
                valid = (inputs == route) & np.isfinite(vector)
                weights = sample_counts * np.outer(valid, valid)
                total += covariance * weights
                counts += weights
        if require_complete and np.any(counts == 0):
            return None
        covariance = np.divide(total, counts, out=np.zeros_like(total), where=counts > 0)
        values, vectors = np.linalg.eigh(covariance)
        return (vectors * np.maximum(values, 0)) @ vectors.conj().T

    def coherent_feeds(self):
        """Reconstruct a common complex field over both antenna feeds.

        Each packet has an unknown common phase and amplitude. Mixed-feed
        observations connect the two feed domains through shared sensor/feed
        samples across packets. Disconnected coverage (including merely
        alternating whole-array R/L) cannot determine relative feed phase.
        Returns a flattened sensor-by-feed field, or None while coverage is
        insufficient. Call with complex amplitudes, not unit phase vectors.
        """
        observations = self._snapshot()
        if len(observations) < 2:
            return None
        n = observations[0][1].size
        data = np.zeros((len(observations), n * 2), complex)
        mask = np.zeros(data.shape, bool)
        for i, (_, values, inputs, _, _) in enumerate(observations):
            valid = np.isfinite(values) & (abs(values) > 0) & np.isin(inputs, (2, 3))
            columns = np.flatnonzero(valid) * 2 + inputs[valid].astype(int) - 2
            data[i, columns] = values[valid]
            mask[i, columns] = True
        counts = mask.sum(axis=0)
        if np.any(counts < 2):
            return None
        connected = mask[0].copy()
        for _ in range(n * 2):
            expanded = connected | np.any(mask[np.any(mask & connected, axis=1)], axis=0)
            if np.array_equal(expanded, connected):
                break
            connected = expanded
        if not connected.all():
            return None
        # Alternating least squares removes each packet's common complex scale.
        field = data[0].copy()
        for _ in range(24):
            denominator = mask @ (abs(field) ** 2)
            scales = (data @ field.conj()) / np.maximum(denominator, 1e-30)
            numerator = np.sum(scales.conj()[:, None] * data, axis=0)
            weights = np.sum(abs(scales[:, None]) ** 2 * mask, axis=0)
            field = numerator / np.maximum(weights, 1e-30)
            field /= max(float(np.linalg.norm(field)), 1e-30)
        # Refit scales after normalization before testing model consistency.
        scales = (data @ field.conj()) / np.maximum(mask @ (abs(field) ** 2), 1e-30)
        error = np.sum(abs((data - scales[:, None] * field) * mask) ** 2)
        if error > .15 * np.sum(abs(data) ** 2):
            return None
        field *= np.exp(-1j * np.angle(field[np.argmax(abs(field))]))
        return field.reshape(n, 2)

    def polarized_covariance(self, feeds, inverse):
        """Transfer measured pair covariances to the reconstructed Jones basis.

        Feed reconstruction supplies the cross-feed phase gauge. Within each
        observed pair the full waveform covariance, including incoherent energy,
        is retained. Weak feed nulls are excluded from this division.
        """
        feeds = np.asarray(feeds)
        field = np.einsum("npq,nq->np", inverse.reshape(-1, 2, 2), feeds)
        n = len(feeds)
        total, counts = np.zeros((n,n), complex), np.zeros((n,n))
        for _, vector, inputs, covariance, sample_counts in self._snapshot():
            selected = feeds[np.arange(n), np.clip(inputs-2, 0, 1).astype(int)]
            valid = np.isin(inputs, (2,3)) & np.isfinite(vector) & (abs(selected) > np.max(abs(feeds)) * .02)
            transform = np.divide(field, selected[:,None], out=np.zeros_like(field), where=valid[:,None])
            weights = sample_counts * np.outer(valid,valid)
            total += covariance * (transform @ transform.conj().T) * weights
            counts += weights
        if np.any(counts == 0):
            return None
        covariance = total / counts
        values, vectors = np.linalg.eigh(covariance)
        return (vectors * np.maximum(values,0)) @ vectors.conj().T
