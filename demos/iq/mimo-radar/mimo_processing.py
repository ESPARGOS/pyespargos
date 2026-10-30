"""Processing for this demo's two interleaved OFDM transmitters.

Uses corner transmitters on a regular planar grid, 256-sample receive windows
and repeating band-limited OFDM pilots. TX runs at 80 MSa/s, with a selectable
receive rate. The library handles waveforms, capture and calibration. This
module owns the demo's channel model,
clutter rejection and range/Doppler/angle displays.
"""

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
import math

import numpy as np
from scipy import fft
from scipy.signal import resample

from espargos.waveforms import IQSamples
from espargos.iq_packet import DECIM_TO_FS

SAMPLES = 256
EDGE_BINS = 8  # Leave 2.5 MHz at each band edge at 80 MSa/s.
RATE = 80e6
C = 299792458.0
INTERVAL = 1792  # Stay below the firmware's 180 accumulated records/s limit.
DOPPLER_WINDOWS = 72
DOPPLER_OVERSAMPLING = 4
COUNTER_MODULUS = 1 << 24  # Counter units are 256 received samples.
SAMPLE_RATES = tuple(sorted(set(DECIM_TO_FS.values()), reverse=True))


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
    def interval_chunks(self):
        # Keep the capture cadence near 174 Hz at every RX rate.
        return max(64, round(INTERVAL * self.rate / RATE))

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
        self.doppler_templates = np.exp(-2j * np.pi * np.arange(history)[:, None] * np.fft.fftfreq(self.doppler_bins)[None, :])
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
        self.range_matrices = tuple(np.ascontiguousarray(self.delay_matrix[mask], dtype=np.complex128) for mask in self.masks)

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

        same_row = self.aperture_y[:, None] == self.aperture_y[None, :]
        separation = self.aperture_x[:, None] - self.aperture_x[None, :]
        self.spatial_pairs = [np.nonzero(same_row & (separation == lag)) for lag in range(self.aperture_shape[1])]

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
        fit = np.linalg.solve(basis.T @ (weight[:, None] * basis), basis.T * weight)
        residual = self.doppler_templates - basis @ (fit @ self.doppler_templates)
        response = weight @ abs(residual) ** 2 / weight.sum()
        scale = np.divide(1, response, out=np.zeros_like(response), where=response > 1e-6)
        return fit, scale

    def maps(self):
        """Compute both power maps from the same capture window."""
        if len(self.frames) < self.history:
            return None

        valid = np.array([v is not None for v in self.frames])
        if valid.mean() < 0.8:
            return None

        data = np.asarray([v if v is not None else np.zeros((self.path_count, SAMPLES), np.complex64) for v in self.frames], dtype=np.complex128)

        weight = self.slow_window * valid
        # The usual complete window has fixed terms. Missing captures need a
        # fresh fit and normalization, without changing the cached full window.
        fit, doppler_scale = self._complete_window_terms if valid.all() else self._slow_time_terms(weight)
        # Split the range window around the clutter filter. Both data and
        # templates must use the same Hermitian weighting to preserve peak range.
        data *= np.sqrt(self.range_weights)[None, None, :]
        energy = self._suppress_leakage(data, weight)
        data *= np.sqrt(self.range_weights)[None, None, :]
        # Remove the remaining stationary signal and smooth drift. Normalize
        # each Doppler template so slow targets keep their velocity peak.
        flat = data.reshape(self.history, -1)
        flat -= self.clutter_basis @ (fit @ flat)

        range_data = np.empty((self.history, self.path_count, len(self.ranges)), np.complex128)
        for tx, (mask, matrix) in enumerate(zip(self.masks, self.range_matrices)):
            paths = slice(tx * self.receiver_count, (tx + 1) * self.receiver_count)
            compact = data[:, paths, :][:, :, mask]
            range_data[:, paths] = (compact.reshape(-1, matrix.shape[0]) @ matrix).reshape(self.history, self.receiver_count, -1)

        template_energy = np.repeat(self.masks @ self.range_weights, self.receiver_count)
        for row in np.unique(self.aperture_y):
            ids = self.aperture_y == row
            range_data[:, ids] /= np.sqrt(np.average(energy[ids], axis=0, weights=template_energy[ids]))[None, None, :]

        # Make slow time contiguous for the Doppler FFT.
        windowed = np.ascontiguousarray(range_data.transpose(1, 2, 0))
        windowed *= weight
        doppler = fft.fft(windowed, n=self.processing_doppler_bins, axis=-1)
        doppler /= weight.sum()
        doppler = doppler.transpose(2, 0, 1)

        rd = np.empty((len(self.ranges), self.doppler_bins), np.float32)
        ra = np.empty((len(self.ranges), len(self.angles)), np.float32)

        # Process eight ranges at a time to keep the working arrays small.
        for start in range(0, len(self.ranges), 8):
            stop = min(start + 8, len(self.ranges))
            block = doppler[:, :, start:stop].transpose(0, 2, 1)
            power = self._beam_power(block, self.doppler_bins) / self.path_count**2
            power *= doppler_scale[:, None, None]
            # Visible FFT bins lie on either side of the unshifted spectrum.
            edge = self.visible_beams.stop - len(self.beam_directions) // 2
            negative_start = self.visible_beams.start + len(self.beam_directions) // 2
            rd[start:stop] = np.maximum(power[..., :edge].max(axis=-1), power[..., negative_start:].max(axis=-1)).T
            ra[start:stop] = power.max(axis=0)

        # One display reference, estimated after beamforming and outside the
        # Doppler speeds normally occupied by hand motion and walking.
        outer = rd[:, abs(np.fft.ifftshift(self.velocities)) > 2.5]
        self.map_noise = max(float(np.median(np.median(outer, axis=1))), 1e-20)

        return np.fft.fftshift(rd, axes=1), np.fft.fftshift(ra, axes=1)

    def _suppress_leakage(self, data, weight):
        """Downweight leakage fluctuations in-place and return template energies."""
        reference = np.einsum("t,tpf->pf", weight, data) / weight.sum()
        # Gain/phase changes follow the stationary spectrum. Small timing
        # changes also multiply it by frequency. Orthonormalize these two shapes.
        q0 = reference / np.maximum(np.linalg.norm(reference, axis=-1, keepdims=True), 1e-20)
        q1 = q0 * (self.offsets / self.sample_rate)
        q1 -= q0 * np.sum(q1 * q0.conj(), axis=-1, keepdims=True)
        q1 /= np.maximum(np.linalg.norm(q1, axis=-1, keepdims=True), 1e-20)
        basis_f = np.stack((q0, q1), axis=1)
        mask = np.repeat(self.masks, self.receiver_count, axis=0)
        coefficients = np.einsum("tpf,pkf->tpk", data - reference, basis_f.conj())
        # Estimate their excess noise outside walking Doppler speeds, rather
        # than treating every moving echo as a change in the hardware.
        spectrum = fft.fft((data - reference) * weight[:, None, None], axis=0) / np.linalg.norm(weight)
        outer = abs(np.fft.fftfreq(self.history, self.dt) * C / (2 * self.frequency)) > 2.5
        projected = np.einsum("tpf,pkf->tpk", spectrum[outer], basis_f.conj())
        variance = np.median(abs(projected) ** 2, axis=0) / np.log(2)
        residual_power = np.sum(abs(spectrum[outer]) ** 2, axis=-1) - np.sum(abs(projected) ** 2, axis=-1)
        white_noise = np.median(residual_power, axis=0) / (mask.sum(axis=-1) - 2)
        # Regularization prevents a numerical null and limits suppression to
        # 40 dB. The relative floor protects nearly noiseless input.
        floor = np.maximum(white_noise, 1e-10 * np.sum(abs(reference) ** 2, axis=-1))
        alpha = np.clip(1 - floor[:, None] / np.maximum(variance, 1e-30), 0, 0.9999)
        self._clutter_weights = alpha
        data -= np.einsum("tpk,pkf->tpf", coefficients * alpha, basis_f)
        # Match the range templates to the SAME weighting. Omitting this
        # normalization pushes nearby echoes toward the first range sidelobe.
        projection = (basis_f * np.sqrt(self.range_weights)[None, None, :]) @ self.delay_matrix
        total_energy = mask @ self.range_weights
        energy = 1 - np.sum(alpha[..., None] * abs(projection) ** 2, axis=1) / total_energy[:, None]
        return energy

    def _beam_power(self, block, doppler_bins=None):
        # Correlate antennas within each row, then add row powers.
        # Interpolation near the clutter notch subtracts nearly equal terms.
        # Double precision keeps normalization from magnifying roundoff there.
        block = np.asarray(block, dtype=np.complex128)
        conjugate = block.conj()
        correlations = np.empty((*block.shape[:2], self.aperture_shape[1]), np.complex128)
        for lag, (left, right) in enumerate(self.spatial_pairs):
            correlations[..., lag] = np.einsum("drp,drp->dr", block[..., left], conjugate[..., right])

        # The 2N Doppler grid retains all correlation lags from N captures.
        # Interpolating these coefficients is cheaper than a full beamspace cube.
        if doppler_bins is not None and doppler_bins != len(correlations):
            correlations = resample(correlations, doppler_bins, axis=0)
        return np.maximum(fft.hfft(correlations, n=self.spatial_fft_shape[1], axis=-1), 0)

    def display_maps(self, maps):
        """Power in dB, using one background reference for both plots."""
        reference = max(self.map_noise, 1e-20)
        result = [10 * np.log10(np.maximum(power / reference, 1e-20)) for power in maps]
        return result[0][:, ::-1], result[1]


def render_maps(processor):
    """Compute one display frame in the plotting process."""
    maps = processor.maps()
    return None if maps is None else processor.display_maps(maps)
