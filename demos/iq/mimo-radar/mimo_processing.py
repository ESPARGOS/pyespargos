"""Processing for this demo's two interleaved OFDM transmitters.

Uses corner transmitters on a regular planar grid, 256-sample receive windows
and repeating band-limited OFDM pilots. TX runs at 80 MSa/s, with a selectable
receive rate. The library handles waveforms, capture and calibration. This
module owns the demo's channel model,
clutter rejection and range/Doppler/angle displays.
"""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
import math
import os

import numpy as np
from scipy import fft
from scipy.signal import resample

from espargos.waveforms import IQSamples

SAMPLES = 256
EDGE_BINS = 8  # Leave 2.5 MHz at each band edge at 80 MSa/s.
RATE = 80e6
C = 299792458.0
DOPPLER_WINDOWS = 72
DOPPLER_OVERSAMPLING = 4
COUNTER_MODULUS = 1 << 24  # Counter units are 256 received samples.
# Active window lengths tested on the 30-receiver array. Short windows let
# the sensor finish the sums and send the record before the next exposure.
ACCUMULATION_CHUNKS = {80e6: 128, 40e6: 224, 20e6: 224, 10e6: 112, 8e6: 90, 4e6: 90}
SAMPLE_RATES = tuple(ACCUMULATION_CHUNKS)


@dataclass(frozen=True)
class Sampling:
    """Sample-rate-dependent settings shared by waveform, capture and display.

    TX runs at 80 MSa/s and RX vectors always contain 256 samples. The pilot
    period divides both the TX buffer and the RX vector duration, including
    at 8/4 MSa/s where the firmware requires a longer power-of-two buffer.
    """

    rate: float = RATE

    def __post_init__(self):
        if self.rate not in SAMPLE_RATES:
            raise ValueError(f"Unsupported RX sample rate: {self.rate}")

    @property
    def sample_step(self):
        return round(RATE / self.rate)

    @property
    def tx_samples(self):
        return 1 << (SAMPLES * self.sample_step - 1).bit_length()

    @property
    def pilot_period(self):
        return math.gcd(self.tx_samples, SAMPLES * self.sample_step)

    @property
    def bin_stride(self):
        return SAMPLES * self.sample_step // self.pilot_period

    @property
    def accumulation_chunks(self):
        return ACCUMULATION_CHUNKS[self.rate]

    @property
    def interval_chunks(self):
        # The sensor admits every Nth window to cap one-lane output at
        # 180 records/s. Doppler uses the distance between admitted windows.
        offered = int(self.rate) // SAMPLES // self.accumulation_chunks
        stride = max(1, math.ceil(offered / 180))
        return stride * self.accumulation_chunks

    @property
    def dt(self):
        return self.interval_chunks * SAMPLES / self.rate

    @property
    def range_maximum(self):
        return 15 * RATE / self.rate

    @property
    def unambiguous_range(self):
        # Each transmitter occupies every second pilot tone.
        return C * SAMPLES / (4 * self.bin_stride * self.rate)

    def display_range(self, requested=0):
        if isinstance(requested, bool) or not math.isfinite(requested) or requested < 0:
            raise ValueError("Range maximum must be a finite nonnegative number (metres)")
        # Keep the endpoint below the repeated zero-delay response.
        return min(requested or self.range_maximum, math.floor(self.unambiguous_range))

    def history(self, seconds):
        return round(seconds / self.dt)


INTERVAL = Sampling().interval_chunks


def corner_transmitters(positions):
    """Select opposite corners of the antenna grid."""
    positions = np.asarray(positions)
    if positions.ndim != 2 or positions.shape[1] != 2 or len(positions) < 3 or not np.all(np.isfinite(positions)):
        raise ValueError("Radar needs at least three antenna positions")

    order = np.lexsort((positions[:, 0], positions[:, 1]))
    return np.array([order[-1], order[0]])


@lru_cache(maxsize=len(SAMPLE_RATES))
def pilots(sample_rate=RATE):
    """Band-limited pilots whose period divides one 256-sample RX capture.

    At 8/4 MSa/s only every fifth RX bin fits the firmware's power-of-two
    replay buffers. Generate that common period, then repeat it exactly.
    """
    sampling = Sampling(sample_rate)
    period, stride = sampling.pilot_period, sampling.bin_stride
    bins = np.arange(math.ceil(-SAMPLES / (2 * stride)), math.ceil(SAMPLES / (2 * stride)))
    bins = bins[abs(bins * stride) <= SAMPLES // 2 - EDGE_BINS]
    result = []
    for parity in range(2):
        active = bins[bins % 2 == parity]
        spectrum = np.zeros(period, complex)
        n = np.arange(len(active))
        spectrum[active % period] = np.exp(1j * np.pi * n * (n + 1) / len(n))

        # Boost the edges to offset some analog filter loss.
        spectrum[active % period] *= 1 + 0.5 * (2 * active * stride / SAMPLES) ** 2
        magnitude = abs(spectrum)

        # Adjust phases to reduce peaks while keeping the subcarrier amplitudes.
        for _ in range(128):
            samples = np.fft.ifft(spectrum)
            limit = 1.15 * np.sqrt(np.mean(abs(samples) ** 2))
            clipped = samples * np.minimum(1, limit / np.maximum(abs(samples), 1e-20))
            spectrum = magnitude * np.exp(1j * np.angle(np.fft.fft(clipped)))

        samples = np.fft.ifft(spectrum)
        result.append(IQSamples(np.tile(samples / np.max(abs(samples)), sampling.tx_samples // period)))
    return result


class MIMOProcessor:
    """Channel estimation and radar maps for the two OFDM transmitters."""

    def __init__(self, positions, frequency=2385e6, history=DOPPLER_WINDOWS, sample_rate=RATE, range_maximum=0):
        positions = np.asarray(positions).reshape(-1, 2)
        self.plot_config = (tuple(map(tuple, positions)), frequency, history, sample_rate, range_maximum)
        self.tx_indices = corner_transmitters(positions)
        self.rx_indices = np.array([i for i in range(len(positions)) if i not in self.tx_indices])
        self.receiver_count = len(self.rx_indices)
        self.path_count = 2 * self.receiver_count

        self.frequency = frequency
        self.sample_rate = sample_rate
        self.sampling = Sampling(sample_rate)
        self.tx_samples = self.sampling.tx_samples
        self.interval_chunks = self.sampling.interval_chunks
        self.history = history
        self.dt = self.sampling.dt
        self.ranges = np.linspace(0, self.sampling.display_range(range_maximum), 121)
        self.doppler_bins = history * DOPPLER_OVERSAMPLING
        self.processing_doppler_bins = 2 * history
        self.velocities = -np.fft.fftshift(np.fft.fftfreq(self.doppler_bins, self.dt)) * C / (2 * frequency)

        self.slow_time = np.linspace(-1, 1, history)
        self.slow_window = np.hanning(history)
        self.clutter_basis = np.polynomial.polynomial.polyvander(self.slow_time, 2)
        self._complete_window_terms = self._slow_time_terms(self.slow_window)

        self.offsets = np.fft.fftfreq(SAMPLES, 1 / sample_rate)
        indices = np.arange(SAMPLES) * self.sampling.sample_step % self.tx_samples
        self.pilots = np.stack([np.fft.fft(p.synthesize(samples=self.tx_samples)[indices]) for p in pilots(sample_rate)])
        bins = np.rint(self.offsets / (sample_rate / SAMPLES)).astype(int)
        stride = self.sampling.bin_stride
        self.masks = np.stack([(bins % stride == 0) & ((bins // stride) % 2 == i) for i in range(2)])
        self.masks[:, abs(bins) > SAMPLES // 2 - EDGE_BINS] = False

        # Discard DC: receiver offset drift would appear at every range.
        self.masks[:, 0] = False
        self.channel_weights = np.zeros((2, SAMPLES), np.complex64)
        for t, mask in enumerate(self.masks):
            pilot = self.pilots[t, mask]
            # Estimate the channel, including removal of the pilot envelope.
            # The clutter and range templates model a flat transmitted spectrum.
            self.channel_weights[t, mask] = ((SAMPLES / 2) / np.count_nonzero(mask)) / pilot

        self.virtual_positions = (positions[self.tx_indices, None, :] + positions[None, self.rx_indices, :]).reshape(self.path_count, 2)
        self.delay_matrix = np.exp(2j * np.pi * self.offsets[:, None] * (2 * self.ranges[None, :] / C)).astype(np.complex64)
        # Each TX uses only its own tones. Keep compact matrices ready for the
        # range transform, including the sparse tone sets at 8/4 MSa/s.
        self.range_matrices = tuple(self.delay_matrix[mask] for mask in self.masks)

        # Find the grid coordinates of each virtual antenna.
        coordinates = []
        spacings = []
        for axis in range(2):
            values = np.unique(np.round(self.virtual_positions[:, axis], 9))
            spacing = np.min(np.diff(values)) if len(values) > 1 else C / (2 * frequency)
            indices = (self.virtual_positions[:, axis] - values[0]) / spacing
            if not np.allclose(indices, np.rint(indices), atol=1e-6):
                raise ValueError("MIMO radar requires a regular antenna grid")
            coordinates.append(np.rint(indices).astype(int))
            spacings.append(spacing)

        self.aperture_spacing = spacings[0]
        self.aperture_x, self.aperture_y = coordinates
        self.aperture_shape = (int(self.aperture_y.max()) + 1, int(self.aperture_x.max()) + 1)
        self.spatial_fft_shape = (self.aperture_shape[0], max(128, 2 * self.aperture_shape[1]))
        self.beam_directions = np.fft.fftshift(np.fft.fftfreq(self.spatial_fft_shape[1], spacings[0])) * C / frequency
        self.beamspace_period = C / (frequency * self.aperture_spacing)
        self.angles = np.rad2deg(np.arcsin(np.clip(self.beam_directions, -1, 1)))
        visible = np.flatnonzero(abs(self.beam_directions) <= 1)
        self.visible_beams = slice(visible[0], visible[-1] + 1)

        # Translating a row changes its beam phase but preserves its power.
        # Batch rows of equal width so the lag sums skip unused aperture cells.
        rows_by_width = {}
        for row in np.unique(self.aperture_y):
            ids = np.flatnonzero(self.aperture_y == row)
            x = self.aperture_x[ids]
            x = x - x.min()
            rows_by_width.setdefault(int(x.max() + 1), []).append((x, ids))
        self.row_groups = []
        for width, rows in rows_by_width.items():
            x = np.concatenate([cols for cols, _ in rows])
            y = np.repeat(np.arange(len(rows)), [len(ids) for _, ids in rows])
            ids = np.concatenate([ids for _, ids in rows])
            self.row_groups.append(((width, len(rows)), (x, y), ids))
        self.has_coincident_paths = len(np.unique(np.column_stack((self.aperture_x, self.aperture_y)), axis=0)) < self.path_count
        self.range_weights = np.ones(SAMPLES)
        self.reset()

    def set_range_response(self, reference):
        """Compensate smooth band-edge rolloff, with at most 3.5 dB boost."""
        mask = np.repeat(self.masks, self.receiver_count, axis=0)
        amplitude = np.sqrt(np.sum(abs(reference) ** 2, axis=0) / np.maximum(mask.sum(axis=0), 1))
        order = np.fft.fftshift(np.arange(SAMPLES))
        valid = mask.any(axis=0)[order]
        bins = np.arange(SAMPLES)
        amplitude = np.interp(bins, bins[valid], amplitude[order][valid])

        # Smooth over one eighth of the receive band to avoid boosting narrow multipath nulls.
        amplitude = np.convolve(np.pad(amplitude, 16, mode="edge"), np.ones(33) / 33, mode="valid")
        peak = amplitude.max()
        self.range_weights = np.fft.ifftshift(np.clip(peak / np.maximum(amplitude, 1e-20), 1, 1.5))

    def reset(self):
        self.frames = deque(maxlen=self.history)
        self.last_index = None
        self.missing = 0

    def channels(self, iq, correction):
        spectrum = fft.fft(iq, axis=-1) * correction
        result = np.empty((2, self.receiver_count, SAMPLES), np.complex64)
        np.multiply(spectrum[None], self.channel_weights[:, None], out=result)
        return result.reshape(self.path_count, SAMPLES)

    def push(self, channels, chunk_index):
        if self.last_index is not None:
            delta = (chunk_index - self.last_index) % COUNTER_MODULUS
            if delta == 0:
                return False
            if delta >= COUNTER_MODULUS // 2:
                age = (self.last_index - chunk_index) % COUNTER_MODULUS
                slot = len(self.frames) - 1 - age // self.interval_chunks
                if age % self.interval_chunks or slot < 0 or self.frames[slot] is not None:
                    return False
                self.frames[slot] = channels.copy()
                self.missing -= 1
                return True
            if delta % self.interval_chunks or delta > self.interval_chunks * self.history:
                self.frames.clear()
            else:
                for _ in range(delta // self.interval_chunks - 1):
                    self.frames.append(None)
                    self.missing += 1

        self.last_index = chunk_index
        self.frames.append(channels.copy())
        return True

    def _slow_time_terms(self, weight):
        """Clutter fit and Doppler normalization for one capture-coverage mask."""
        basis = self.clutter_basis
        weighted_basis = basis.T * weight
        gram = weighted_basis @ basis
        fit = np.linalg.solve(gram, weighted_basis)
        # Each unit-amplitude Doppler tone has total weighted power one.
        # Subtract its projection onto the polynomial clutter basis. The FFT
        # evaluates the three fit coefficients for every tone simultaneously.
        coefficients = fft.fft(fit, n=self.doppler_bins, axis=-1)
        projected_energy = (coefficients.conj() * (gram @ coefficients)).sum(axis=0).real
        response = 1 - projected_energy / weight.sum()
        scale = np.divide(1, response, out=np.zeros_like(response), where=response > 1e-6)
        return fit, scale

    def capture_window(self):
        """Copy just the measured OFDM tones into a self-contained plot window."""
        if len(self.frames) < self.history:
            return None
        valid = np.array([frame is not None for frame in self.frames])
        if valid.mean() < 0.8:
            return None
        dtype = np.result_type(*(frame.dtype for frame in self.frames if frame is not None))
        bands = []
        for tx, mask in enumerate(self.masks):
            paths = slice(tx * self.receiver_count, (tx + 1) * self.receiver_count)
            data = np.zeros((self.receiver_count, self.history, np.count_nonzero(mask)), dtype)
            for t, frame in enumerate(self.frames):
                if frame is not None:
                    data[:, t] = frame[paths, :][:, mask]
            bands.append(data)
        return valid, bands

    def maps(self):
        """Compute both power maps from the same capture window."""
        window = self.capture_window()
        if window is None:
            return None
        return self._window_maps(*window)

    def _window_maps(self, valid, bands):
        weight = self.slow_window * valid
        # The usual complete window has fixed terms. Missing captures need a
        # fresh fit and normalization, without changing the cached full window.
        fit, doppler_scale = self._complete_window_terms if valid.all() else self._slow_time_terms(weight)
        # The two pilot bands and the disjoint range blocks are independent.
        # Bound parallelism so live IQ reception still gets CPU time.
        with ThreadPoolExecutor(max_workers=min(3, os.cpu_count() or 1)) as workers:
            jobs = [workers.submit(self._range_profile, tx, tones, weight, fit) for tx, tones in enumerate(bands)]
            profiles, energies, clutter_weights = zip(*(job.result() for job in jobs))
            self._clutter_weights = np.concatenate(clutter_weights)
            doppler = self._doppler(np.concatenate(profiles), np.concatenate(energies), weight)
            return self._display_power(doppler, doppler_scale, workers)

    def _doppler(self, profiles, energy, weight):
        # Match the range template normalization to the filtered signal, using
        # one power normalization per virtual row to preserve its beam shape.
        template_energy = np.repeat(self.masks @ self.range_weights, self.receiver_count)
        row_total = np.bincount(self.aperture_y, weights=template_energy, minlength=self.aperture_shape[0])
        row_energy = np.zeros((len(row_total), len(self.ranges)))
        np.add.at(row_energy, self.aperture_y, energy * template_energy[:, None])
        path_scale = np.sqrt(row_total[self.aperture_y, None] / row_energy[self.aperture_y])
        # Interpolation at the clutter notch needs a double-precision Doppler FFT.
        windowed = np.ascontiguousarray(profiles.transpose(0, 2, 1), dtype=np.complex128)
        windowed *= weight / weight.sum()
        windowed *= path_scale[:, :, None]
        return fft.fft(windowed, n=self.processing_doppler_bins, axis=-1).transpose(2, 0, 1)

    def _display_power(self, doppler, doppler_scale, workers):
        rd = np.empty((len(self.ranges), self.doppler_bins), np.float32)
        ra = np.empty((len(self.ranges), len(self.angles)), np.float32)
        scale = doppler_scale / self.path_count**2
        # The visible angles straddle the wrap in the unshifted FFT.
        positive_end = self.visible_beams.stop - len(self.angles) // 2
        negative_start = self.visible_beams.start + len(self.angles) // 2

        # Eight ranges at the default integration time, fewer for longer FFTs
        # so each thread's beamspace cube still fits in cache.
        cells_per_block = 8 * DOPPLER_WINDOWS * DOPPLER_OVERSAMPLING
        block_size = max(2, min(16, cells_per_block // self.doppler_bins))

        def render_block(start):
            ranges = slice(start, start + block_size)
            block = doppler[:, :, ranges].transpose(0, 2, 1)
            power = self._beam_power(block, self.doppler_bins, scale)
            rd[ranges] = np.maximum(power[..., :positive_end].max(axis=-1), power[..., negative_start:].max(axis=-1)).T
            ra[ranges] = power.max(axis=0)

        list(workers.map(render_block, range(0, len(self.ranges), block_size)))
        return np.fft.fftshift(rd, axes=1), np.fft.fftshift(ra, axes=1)

    def _range_profile(self, tx, tones, weight, fit):
        """Filter one transmitter's active tones and match them to range templates."""
        mask, matrix = self.masks[tx], self.range_matrices[tx]
        band = np.sqrt(self.range_weights[mask])
        # Subtract the stationary return in double precision before casting
        # the much smaller residual. Split the band weighting around whitening.
        reference = np.einsum("t,ptf->pf", weight / weight.sum(), tones)
        data = (tones - reference[:, None]).astype(np.complex64)
        data *= band
        reference *= band
        basis, alpha = self._suppress_leakage(data, reference, self.offsets[mask], weight)
        data *= band
        # Reject the remaining smooth slow-time drift before matching ranges.
        data -= self.clutter_basis.astype(np.float32) @ (fit.astype(np.float32) @ data)

        projection = (basis * band) @ matrix
        energy = 1 - np.sum(alpha[..., None] * abs(projection) ** 2, axis=1) / self.range_weights[mask].sum()
        return data @ matrix, energy, alpha

    def _suppress_leakage(self, data, reference, offsets, weight):
        """Suppress fluctuations parallel to the stationary return and its delay derivative."""
        q0 = reference / np.maximum(np.linalg.norm(reference, axis=-1, keepdims=True), 1e-20)
        q1 = q0 * (offsets / self.sample_rate)
        q1 -= q0 * np.sum(q1 * q0.conj(), axis=-1, keepdims=True)
        q1 /= np.maximum(np.linalg.norm(q1, axis=-1, keepdims=True), 1e-20)
        basis = np.stack((q0, q1), axis=1).astype(np.complex64)
        coefficients = data @ basis.conj().transpose(0, 2, 1)

        # A hardware clock shift has an imaginary slope. Moving echoes rotate
        # through both quadratures. Regress out jitter only if it dominates by 10 dB.
        slope = coefficients[..., 1]
        delay = slope.imag
        power = np.einsum("pt,t,pt->p", delay, weight, delay)
        quadrature_power = np.einsum("pt,t,pt->p", slope.real, weight, slope.real)
        confidence = np.clip(1 - 10 * quadrature_power / np.maximum(power, 1e-30), 0, 1)
        if confidence.any():
            drift_fit = np.einsum("t,pt,ptk->pk", weight, delay, coefficients)
            drift_fit *= (confidence / np.maximum(power, 1e-30))[:, None]
            drift = (delay[..., None] * drift_fit[:, None]).astype(np.complex64)
            data -= drift @ basis
            coefficients -= drift

        # A fixed basis commutes with the temporal FFT. Transform the two
        # coefficients directly instead of projecting every Doppler bin again.
        outer = abs(np.fft.fftfreq(self.history, self.dt) * C / (2 * self.frequency)) > 2.5
        taper = (weight / np.linalg.norm(weight)).astype(np.float32)[None, :, None]
        spectrum = fft.fft(data * taper, axis=1)[:, outer]
        projected = fft.fft(coefficients * taper, axis=1)[:, outer]
        projected_power = abs(projected) ** 2
        variance = np.median(projected_power, axis=1) / np.log(2)
        residual_power = np.sum(abs(spectrum) ** 2, axis=-1) - projected_power.sum(axis=-1)
        white_noise = np.median(residual_power, axis=1) / (len(offsets) - 2)
        # Bound suppression to 40 dB and keep a relative floor for quiet inputs.
        floor = np.maximum(white_noise, 1e-10 * np.sum(abs(reference) ** 2, axis=-1))
        alpha = np.clip(1 - floor[:, None] / np.maximum(variance, 1e-30), 0, 0.9999)
        data -= (coefficients * alpha.astype(np.float32)[:, None]) @ basis
        return basis, alpha

    def _beam_power(self, block, doppler_bins=None, scale=None):
        # Correlate antennas within each row, then add row powers.
        # Interpolation near the clutter notch subtracts nearly equal terms.
        # Double precision keeps normalization from magnifying roundoff there.
        n_doppler, n_range, _ = block.shape
        paths = block.transpose(2, 1, 0)
        correlations = np.zeros((self.aperture_shape[1], n_range, n_doppler), np.complex128)
        for shape, cells, path_ids in self.row_groups:
            width = shape[0]
            grid = np.zeros((*shape, n_range, n_doppler), np.complex128)
            if self.has_coincident_paths:
                np.add.at(grid, cells, paths[path_ids])
            else:
                grid[cells] = paths[path_ids]
            conjugate = grid.conj()
            for lag in range(width):
                correlations[lag] += np.einsum("xyrd,xyrd->rd", grid[lag:], conjugate[: width - lag])

        # N captures have correlation lags from -(N-1) to N-1. The 2N
        # Doppler grid retains all of them, so interpolate before the angle FFT.
        if doppler_bins is not None and doppler_bins != n_doppler:
            correlations = resample(correlations, doppler_bins, axis=-1)
        if scale is not None:
            correlations *= scale
        angular = np.ascontiguousarray(correlations.transpose(2, 1, 0), dtype=np.complex64)
        return np.maximum(fft.hfft(angular, n=self.spatial_fft_shape[1], axis=-1), 0)

    def background_power(self, range_doppler):
        """Measure the noise floor outside typical hand and walking speeds."""
        outer = range_doppler[:, abs(self.velocities) > 2.5]
        return max(float(np.median(np.median(outer, axis=1))), 1e-20)

    def display_maps(self, maps):
        """Power in dB, using one background reference for both plots."""
        reference = self.background_power(maps[0])
        result = [10 * np.log10(np.maximum(power / reference, 1e-20)) for power in maps]
        return result[0][:, ::-1], result[1]


# Each plotting process retains only the model for its current settings.
_plot_processor = lru_cache(maxsize=1)(MIMOProcessor)


def render_maps(config, range_weights, window):
    """Compute one display frame in the plotting process."""
    processor = _plot_processor(*config)
    processor.range_weights = range_weights
    return processor.display_maps(processor._window_maps(*window))
