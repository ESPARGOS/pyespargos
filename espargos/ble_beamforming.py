"""Stateful BLE direction and polarization estimation, independent of rendering."""

from dataclasses import dataclass, field

import numpy as np
from .ble_phase_history import BLEPhaseHistory
from .array_processing import music_spectrum, root_music_2d, spatial_smoothing_2d, estimate_signal_count
from . import constants
from .polarization import inverse_jones_response


@dataclass(frozen=True)
class BLEBeamformingConfig:
    method: str = "Bartlett"
    history_size: int = 16
    history_age: float = 0.25
    polarization: bool = False
    multipath: bool = True

    def __post_init__(self):
        if self.method not in ("Bartlett", "MUSIC", "Root-MUSIC"):
            raise ValueError("Unknown BLE beamforming method")
        if type(self.history_size) is not int or self.history_size < 1 or not np.isfinite(self.history_age) or self.history_age <= 0:
            raise ValueError("History size and age must be positive")


@dataclass(frozen=True)
class BLEBeamformingResult:
    """A grid spectrum or fitted paths in horizontal/vertical direction cosines.

    Path strengths retain the estimator's relative scale. Jones vectors, when
    available, follow the supplied steering grid. ``path_jones`` holds jointly
    fitted H/V polarization vectors for resolved MUSIC paths, with an arbitrary
    common phase per path. No display kernels are applied.
    """

    spectrum: np.ndarray | None = None
    directions: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))
    strengths: np.ndarray = field(default_factory=lambda: np.empty(0))
    jones: np.ndarray | None = None
    path_jones: np.ndarray | None = None


def _fit_path_polarizations(field, directions, frequency):
    """Separate each resolved path's complex H/V amplitudes jointly.

    A Bartlett projection at one path also contains the other paths' sidelobes.
    Joint fitting allows different arbitrary Jones vectors for every path.
    """
    if not len(directions):
        return None
    rows, columns = np.indices(field.shape[:2])
    scale = 2*np.pi*constants.ANTENNA_SEPARATION*frequency/constants.SPEED_OF_LIGHT
    manifold = np.exp(1j*scale*(columns.ravel()[:,None]*directions[:,0]
                                + rows.ravel()[:,None]*directions[:,1]))
    if np.linalg.cond(manifold) > 1e3:
        return None
    fields = np.linalg.lstsq(manifold,field.reshape(-1,2),rcond=None)[0]
    return fields / np.maximum(np.linalg.norm(fields,axis=-1,keepdims=True),1e-30)


def _music_peaks(power, count):
    padded = np.pad(power,1,constant_values=-np.inf)
    peaks = power >= .1*power.max()
    for row in range(3):
        for column in range(3):
            if (row,column) != (1,1):
                peaks &= power > padded[row:row+power.shape[0],column:column+power.shape[1]]
    indices = np.argwhere(peaks)
    if len(indices):
        indices = indices[np.argsort(power[tuple(indices.T)])[::-1]][:count]
    return indices


class BLEBeamformer:
    """Keep independent bounded observation histories for hashable source IDs.

    Call from one processing worker. Supply a clock for offline replay using
    the same time base as observation timestamps.
    """

    def __init__(self, *, clock=None):
        self._clock = clock
        self._histories = {}
        self._polarized_sources = set()
        self._config = None

    def clear(self):
        self._histories.clear()
        self._polarized_sources.clear()

    def process(self, identity, vector, inputs, received, steering, frequency, config: BLEBeamformingConfig, *, amplitude=None, orientations=None, covariance=None, sample_counts=None) -> BLEBeamformingResult:
        vector, inputs, steering = np.asarray(vector), np.asarray(inputs), np.asarray(steering)
        if vector.ndim != 2 or inputs.shape != vector.shape or steering.ndim != 4 or steering.shape[:2] != vector.shape:
            raise ValueError("Phase, RF input and steering must use the same two-dimensional sensor layout")
        if amplitude is not None and np.shape(amplitude) != vector.shape:
            raise ValueError("Amplitude must match the sensor layout")
        settings = (config, frequency, steering.shape, vector.shape)
        if settings != self._config:
            self.clear()
            self._config = settings
        self._histories = {key: value for key, value in self._histories.items() if value.snapshot_count}
        self._polarized_sources.intersection_update(self._histories)
        if identity not in self._histories:
            self._histories[identity] = BLEPhaseHistory(size=config.history_size, max_age=config.history_age, clock=self._clock)
        backlog = self._histories[identity]
        shape = steering.shape[2:]
        empty = BLEBeamformingResult()
        kind = config.method
        polarized = config.polarization
        field = vector if amplitude is None else vector * amplitude
        scale = np.sqrt(np.mean(abs(field[np.isfinite(field)]) ** 2)) if np.any(np.isfinite(field)) else 0
        field = field / max(float(scale), 1e-30)
        backlog.append(field, inputs, timestamp=received,
                       covariance=None if covariance is None else np.asarray(covariance) / max(float(scale)**2, 1e-60),
                       sample_counts=sample_counts)
        polarized = polarized and amplitude is not None and orientations is not None
        jones = None
        covariance = None
        if polarized:
            feeds = backlog.coherent_feeds()
            polarized = feeds is not None
        if config.polarization and not polarized and identity in self._polarized_sources:
            # Do not alternate corrected and uncorrected covariance models when
            # a short random-feed history temporarily loses coverage. Consumers
            # can retain the previous result subject to their usual age limit.
            return empty
        if polarized:
            inverse = inverse_jones_response(orientations, np.array([frequency]))[..., 0, :, :]
            field = np.einsum("rcpq,rcq->rcp", inverse, feeds.reshape(*vector.shape, 2))
            covariance = backlog.polarized_covariance(feeds, inverse)
            beams = np.einsum("rcae,rcp->aep", steering.conj(), field) / vector.size
            jones = beams / np.maximum(np.linalg.norm(beams, axis=-1, keepdims=True), 1e-30)
            gauge = np.take_along_axis(jones, np.argmax(abs(jones), axis=-1)[..., None], axis=-1)
            jones *= np.exp(-1j * np.angle(gauge))
        if not polarized and kind == "Bartlett":
            covariance = backlog.covariance(require_complete=False)
        elif not polarized:
            covariance = backlog.covariance()
        if covariance is None:
            return empty
        if polarized:
            self._polarized_sources.add(identity)
        if kind == "Bartlett":
            grid = steering.reshape(vector.size, *shape)
            power = np.maximum(np.einsum("iae,ij,jae->ae", grid.conj(), covariance, grid).real, 0) / vector.size**2
            return BLEBeamformingResult(spectrum=power, jones=jones)
        rows, columns = vector.shape
        if config.multipath:
            covariance, rows, columns = spatial_smoothing_2d(covariance, rows, columns)
        if kind == "MUSIC":
            grid = steering[:rows, :columns]
            # Match Root-MUSIC's useful mode range. Random-feed reconstruction
            # and calibration residue must not consume extra signal dimensions.
            count = estimate_signal_count(np.linalg.eigvalsh(covariance), dynamic_range_db=10) if config.multipath else 1
            power = music_spectrum(covariance, grid, source_count=count)
            peaks = _music_peaks(power, count)
            directions = np.empty((0,2))
            path_jones = None
            if len(peaks) and min(vector.shape) >= 2:
                # Read physical direction cosines from the supplied steering
                # vectors, supporting either endpoint or pixel-center grids.
                scale = 2*np.pi*constants.ANTENNA_SEPARATION*frequency/constants.SPEED_OF_LIGHT
                directions = np.array([[np.angle(steering[0,1,a,e]/steering[0,0,a,e])/scale,
                                        np.angle(steering[1,0,a,e]/steering[0,0,a,e])/scale] for a,e in peaks])
                physical = np.sum(directions**2,axis=1) <= 1+1e-7
                directions, peaks = directions[physical], peaks[physical]
                if polarized:
                    path_jones = _fit_path_polarizations(field,directions,frequency)
            else:
                peaks = np.empty((0,2),int)
            return BLEBeamformingResult(spectrum=power / max(float(power.max()), 1e-30),
                directions=directions, strengths=power[tuple(peaks.T)] if len(peaks) else np.empty(0),
                jones=jones, path_jones=path_jones)
        if kind != "Root-MUSIC":
            raise ValueError(f"Unknown BLE beamformer: {kind}")
        directions, powers, _ = root_music_2d(covariance, rows, columns, frequency_hz=frequency, snapshots=backlog.snapshot_count, source_count=None if config.multipath else 1, dynamic_range_db=10)
        # Discard very weak fitted paths, rather than giving every polynomial
        # root a bright marker. A path is not necessarily the direct transmitter.
        keep = (powers > 0) & (powers >= np.max(powers, initial=0) * 0.1)
        directions, powers = directions[keep], powers[keep]
        return BLEBeamformingResult(directions=directions, strengths=powers, jones=jones,
            path_jones=_fit_path_polarizations(field,directions,frequency) if polarized else None)
