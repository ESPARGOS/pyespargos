"""Moving point targets must keep their range after stationary clutter removal."""

import importlib.util
from pathlib import Path
import unittest

import numpy as np

spec = importlib.util.spec_from_file_location("mimo_processing", Path(__file__).resolve().parents[1] / "demos/iq/mimo-radar/mimo_processing.py")
radar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radar)


class RangeDopplerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        positions = [(x * 0.06, y * 0.06) for y in range(2) for x in range(4)]
        cls.processor = radar.MIMOProcessor(positions, history=72)

    def capture(self, distance, velocity, missing=False, amplitude=0.05):
        p = self.processor
        time = np.arange(p.history) * p.dt
        motion = np.exp(-2j * np.pi * 2 * p.frequency * velocity / radar.C * time)
        response = np.exp(-2j * np.pi * p.offsets * (2 * distance / radar.C))
        mask = np.repeat(p.masks, p.receiver_count, axis=0)
        # Strong direct coupling at zero delay, with a weaker moving echo.
        frames = (mask[None] * (1 + amplitude * motion[:, None, None] * response[None, None])).astype(np.complex64)
        p.reset()
        p.frames.extend(None if missing and i % 7 == 0 else frame for i, frame in enumerate(frames))
        return p.maps()

    def test_nearby_targets_do_not_jump_to_a_bandwidth_bin(self):
        p = self.processor
        for distance in [0, 0.3, 0.7, 1.1, 1.6, 1.95, 3.4]:
            with self.subTest(distance=distance):
                rd, ra = self.capture(distance, 0.5)
                r, d = np.unravel_index(np.argmax(rd), rd.shape)
                self.assertLessEqual(abs(p.ranges[r] - distance), 0.0625)
                self.assertLessEqual(abs(p.ranges[np.argmax(ra.max(axis=1))] - distance), 0.0625)
                self.assertLessEqual(abs(p.velocities[d] - 0.5), abs(p.velocities[1] - p.velocities[0]))

    def test_off_bin_doppler_and_missing_captures(self):
        p = self.processor
        for velocity in [-0.42, 0.37, 1.13]:
            with self.subTest(velocity=velocity):
                rd, _ = self.capture(0.7, velocity, missing=True)
                r, d = np.unravel_index(np.argmax(rd), rd.shape)
                self.assertLessEqual(abs(p.ranges[r] - 0.7), 0.0625)
                self.assertLessEqual(abs(p.velocities[d] - velocity), abs(p.velocities[1] - p.velocities[0]))

    def test_stationary_coupling_is_removed(self):
        rd, ra = self.capture(0, 0, missing=True, amplitude=0)
        # Single-precision cancellation leaves a tiny numerical residue.
        self.assertLess(float(rd.max()), 1e-9)
        self.assertLess(float(ra.max()), 1e-9)

    def simulate(self, targets=(), noise=0, drift=0, seed=1729):
        p = self.processor
        t = (np.arange(p.history) - (p.history - 1) / 2) * p.dt
        mask = np.repeat(p.masks, p.receiver_count, axis=0)
        data = np.broadcast_to(mask, (p.history, *mask.shape)).astype(complex).copy()
        data *= np.exp(1j * drift * t[:, None, None])
        for distance, velocity, angle, amplitude in targets:
            spatial = np.exp(2j * np.pi * p.frequency / radar.C * p.virtual_positions[:, 0] * np.sin(np.deg2rad(angle)))
            delay = np.exp(-2j * np.pi * p.offsets * 2 * distance / radar.C)
            motion = np.exp(-2j * np.pi * 2 * p.frequency / radar.C * velocity * t)
            data += amplitude * mask * motion[:, None, None] * spatial[None, :, None] * delay[None, None, :]
        rng = np.random.default_rng(seed)
        data += noise / np.sqrt(2) * mask * (rng.normal(size=data.shape) + 1j * rng.normal(size=data.shape))
        p.reset()
        p.frames.extend(data.astype(np.complex64))
        return p.maps()

    def test_slow_target_doppler_is_not_pushed_outward(self):
        p = self.processor
        for velocity in [-0.1, 0.1, 0.2]:
            with self.subTest(velocity=velocity):
                rd, _ = self.capture(0.7, velocity)
                r, d = np.unravel_index(rd.argmax(), rd.shape)
                self.assertLessEqual(abs(p.velocities[d] - velocity), abs(p.velocities[1] - p.velocities[0]) / 2)
                self.assertLessEqual(abs(p.ranges[r] - 0.7), 0.0625)

    def test_noise_and_slow_coupling_drift(self):
        p = self.processor
        for seed in range(5):
            for drift in [0, 0.03, 0.1]:
                with self.subTest(seed=seed, drift=drift):
                    displayed = p.display_maps(self.simulate(noise=0.001, drift=drift, seed=seed))
                    # At the default 12 dB threshold, modest drift must not fill
                    # the maps. A small residual near zero velocity is allowed.
                    for power in displayed:
                        self.assertLess(np.mean(power > 12), 0.02)

    def test_two_targets_at_different_ranges_and_directions(self):
        p = self.processor
        targets = [(0.7, 0.37, 25, 0.05), (5, -0.6, -30, 0.04)]
        rd, ra = self.simulate(targets, noise=0.001, drift=0.1)
        for distance, velocity, angle, _ in targets:
            d = np.argmin(abs(p.velocities - velocity))
            r = rd[:, d].argmax()
            self.assertLessEqual(abs(p.ranges[r] - distance), 0.0625)
            self.assertLess(abs(p.angles[ra[r].argmax()] - angle), 2)

    def test_two_targets_at_the_same_range(self):
        from scipy.signal import find_peaks

        p = self.processor
        rd, ra = self.simulate([(3, 0.45, 30, 0.05), (3, -0.6, -30, 0.04)], noise=0.001)
        r = np.argmin(abs(p.ranges - 3))
        velocities = p.velocities[find_peaks(rd[r], height=rd[r].max() * 0.4)[0]]
        angles = p.angles[find_peaks(ra[r], height=ra[r].max() * 0.4)[0]]
        for velocity, angle in [(0.45, 30), (-0.6, -30)]:
            self.assertLess(min(abs(velocities - velocity)), 0.04)
            self.assertLess(min(abs(angles - angle)), 2)

    def test_range_separation_and_unresolved_targets(self):
        from scipy.signal import find_peaks

        p = self.processor
        rd, _ = self.simulate([(1, 0.5, 0, 0.05), (6, 0.5, 0, 0.04)])
        profile = rd[:, np.argmin(abs(p.velocities - 0.5))]
        peaks = p.ranges[find_peaks(profile, height=profile.max() * 0.4)[0]]
        for distance in [1, 6]:
            # Coherent sidelobes from the other target can shift a peak slightly.
            self.assertLessEqual(min(abs(peaks - distance)), 0.25)
        rd, _ = self.simulate([(1, 0.5, 0, 0.05), (1.6, 0.5, 0, 0.04)])
        profile = rd[:25, np.argmin(abs(p.velocities - 0.5))]
        self.assertEqual(len(find_peaks(profile, height=profile.max() * 0.4)[0]), 1)

    def test_interleaved_pilots_preserve_each_channel_phase(self):
        p = self.processor
        channel = np.exp(-2j * np.pi * p.offsets[None, :] * 2 * np.array([0.7, 4])[:, None] / radar.C)
        spectrum = np.sum(p.pilots * channel, axis=0)
        iq = np.broadcast_to(np.fft.ifft(spectrum), (p.receiver_count, radar.SAMPLES))
        measured = p.channels(iq, np.ones_like(iq)).reshape(2, p.receiver_count, radar.SAMPLES)
        for tx in range(2):
            relative = measured[tx, 0, p.masks[tx]] / channel[tx, p.masks[tx]]
            np.testing.assert_allclose(np.angle(relative), 0, atol=1e-6)
            np.testing.assert_allclose(relative, relative.mean(), rtol=1e-6, atol=1e-6)
            np.testing.assert_array_equal(measured[tx, :, ~p.masks[tx]], 0)

    def test_optimized_maps_match_direct_doppler_and_beamforming(self):
        p = self.processor
        actual_rd, actual_ra = self.simulate([(0.7, 0.37, 25, 0.05), (5, -0.6, -30, 0.04)], noise=0.001)
        self.assert_direct_maps_match((actual_rd, actual_ra))

    def test_sparse_range_transform_and_cached_terms_match_direct_processing(self):
        positions = [(x * 0.06, y * 0.06) for y in range(2) for x in range(4)]
        for rate, history in [(80e6, 72), (8e6, 73), (4e6, 72)]:
            self.processor = radar.MIMOProcessor(positions, sample_rate=rate, history=history)
            p = self.processor
            scale = radar.RATE / rate
            self.simulate([(0.7 * scale, 0.37, 25, 0.05), (5 * scale, -0.6, -30, 0.04)], noise=0.001)
            frames = list(p.frames)
            # Change the gap pattern and range weighting, then return to a
            # complete window. Cached terms must not retain the missing data.
            for gap in [None, 0, 2, None]:
                with self.subTest(rate=rate, gap=gap):
                    p.range_weights = np.linspace(1, 1.5 if gap is None else 1.2, radar.SAMPLES)
                    p.frames.clear()
                    p.frames.extend(None if gap is not None and i % 7 == gap else frame for i, frame in enumerate(frames))
                    self.assert_direct_maps_match(p.maps())

    def assert_direct_maps_match(self, maps):
        p = self.processor
        actual_rd, actual_ra = maps
        valid = np.array([frame is not None for frame in p.frames])
        data = np.asarray([frame if frame is not None else np.zeros((p.path_count, radar.SAMPLES)) for frame in p.frames], dtype=np.complex128)
        data *= np.sqrt(p.range_weights)
        weight = np.hanning(p.history) * valid
        reference = np.average(data, axis=0, weights=weight)
        mask = np.repeat(p.masks, p.receiver_count, axis=0)
        energy = np.ones((p.path_count, len(p.ranges)))
        for path in range(p.path_count):
            q0 = reference[path] / np.linalg.norm(reference[path])
            q1 = q0 * p.offsets / radar.RATE
            q1 -= q0 * np.vdot(q0, q1)
            q1 /= np.linalg.norm(q1)
            basis_f = np.stack([q0, q1], axis=1)
            whitening = np.eye(radar.SAMPLES) - (basis_f * p._clutter_weights[path]) @ basis_f.conj().T
            data[:, path] = data[:, path] @ whitening.T
            templates = mask[path, :, None] * np.sqrt(p.range_weights)[:, None] * p.delay_matrix.conj()
            energy[path] = np.einsum("fr,fr->r", templates.conj(), whitening @ templates).real / (mask[path] @ p.range_weights)
        data *= np.sqrt(p.range_weights)
        time = np.arange(p.history) - (p.history - 1) / 2
        basis = np.stack([np.ones_like(time), time, time**2], axis=1)
        design = np.sqrt(weight)[:, None] * basis
        fit = np.linalg.lstsq(design, np.sqrt(weight)[:, None] * data.reshape(p.history, -1), rcond=None)[0]
        residual = (data.reshape(p.history, -1) - basis @ fit).reshape(data.shape)
        ranged = residual @ p.delay_matrix
        for row in np.unique(p.aperture_y):
            ids = p.aperture_y == row
            ranged[:, ids] /= np.sqrt(np.average(energy[ids], axis=0, weights=mask[ids] @ p.range_weights))[None, None, :]
        doppler = np.fft.fft(ranged * weight[:, None, None], n=p.doppler_bins, axis=0) / weight.sum()
        directions = np.fft.ifftshift(p.beam_directions)
        steering = np.exp(-2j * np.pi * p.virtual_positions[:, 0, None] * directions[None, :] * p.frequency / radar.C)
        power = np.zeros((p.doppler_bins, len(p.ranges), len(p.angles)))
        for row in np.unique(p.virtual_positions[:, 1]):
            ids = np.flatnonzero(np.isclose(p.virtual_positions[:, 1], row))
            power += abs(np.einsum("dpr,pa->dra", doppler[:, ids], steering[ids])) ** 2 / p.path_count**2
        templates = np.exp(-2j * np.pi * np.arange(p.history)[:, None] * np.fft.fftfreq(p.doppler_bins)[None, :])
        fit = np.linalg.lstsq(design, np.sqrt(weight)[:, None] * templates, rcond=None)[0]
        energy = np.sum(weight[:, None] * abs(templates - basis @ fit) ** 2, axis=0) / weight.sum()
        power *= np.divide(1, energy, out=np.zeros_like(energy), where=energy > 1e-6)[:, None, None]
        expected_rd = np.fft.fftshift(power[:, :, abs(directions) <= 1].max(axis=-1).T, axes=1)
        expected_ra = np.fft.fftshift(power.max(axis=0), axes=1)
        for actual, expected in [(actual_rd, expected_rd), (actual_ra, expected_ra)]:
            np.testing.assert_allclose(actual, expected, rtol=2e-5, atol=2e-5 * expected.max())

    def test_half_bins_in_range_and_doppler(self):
        p = self.processor
        step = abs(p.velocities[1] - p.velocities[0])
        for distance in [0.0625, 0.9375, 6.0625, 14.9375]:
            for velocity in step * np.array([-13.5, -2.5, 2.5, 13.5]):
                with self.subTest(distance=distance, velocity=velocity):
                    rd, _ = self.simulate([(distance, velocity, 23.7, 0.01)])
                    r, d = np.unravel_index(rd.argmax(), rd.shape)
                    self.assertLessEqual(abs(p.ranges[r] - distance), 0.0625 + 1e-8)
                    self.assertLessEqual(abs(p.velocities[d] - velocity), step / 2 + 1e-8)

    def test_off_bin_peak_power_matches_continuous_target(self):
        p = self.processor
        t = np.arange(p.history) * p.dt
        weight = np.hanning(p.history)
        basis = np.polynomial.polynomial.polyvander(p.slow_time, 2)
        design = np.sqrt(weight)[:, None] * basis
        mask = np.repeat(p.masks, p.receiver_count, axis=0)
        spatial_gain = sum(mask[p.aperture_y == row].sum() ** 2 for row in np.unique(p.aperture_y)) / p.path_count**2
        native_step = abs(p.velocities[1] - p.velocities[0]) * radar.DOPPLER_OVERSAMPLING
        for fraction in np.linspace(0, 1, 9):
            velocity = (2 + fraction) * native_step
            rd, _ = self.simulate([(0.9375, velocity, 23.7, 0.01)])
            motion = np.exp(-2j * np.pi * 2 * p.frequency / radar.C * velocity * t)
            fit = np.linalg.lstsq(design, np.sqrt(weight) * motion, rcond=None)[0]
            surviving_energy = np.sum(weight * abs(motion - basis @ fit) ** 2) / weight.sum()
            exact_power = 0.01**2 * spatial_gain * surviving_energy
            error_db = 10 * np.log10(rd.max() / exact_power)
            self.assertGreater(error_db, -0.15)
            self.assertLess(error_db, 0.01)

    def test_two_off_bin_targets_at_the_same_velocity(self):
        from scipy.signal import find_peaks

        p = self.processor
        velocity = 2.5 * radar.DOPPLER_OVERSAMPLING * abs(p.velocities[1] - p.velocities[0])
        rd, ra = self.simulate([(0.9375, velocity, 23.7, 0.05), (6.0625, velocity, -31.2, 0.035)], noise=0.0001)
        d = np.argmin(abs(p.velocities - velocity))
        peaks = p.ranges[find_peaks(rd[:, d], height=rd[:, d].max() * 0.3)[0]]
        for distance in [0.9375, 6.0625]:
            self.assertLessEqual(min(abs(peaks - distance)), 0.1875)

    def test_doppler_wrap_preserves_off_bin_targets(self):
        p = self.processor
        limit = radar.C / (4 * p.frequency * p.dt)
        for velocity in [limit - 0.013, limit + 0.013, -limit - 0.013, -limit + 0.013]:
            rd, _ = self.simulate([(6.0625, velocity, 23.7, 0.01)])
            r, d = np.unravel_index(rd.argmax(), rd.shape)
            error = (p.velocities[d] - velocity + limit) % (2 * limit) - limit
            self.assertLessEqual(abs(error), abs(p.velocities[1] - p.velocities[0]) / 2)
            self.assertLessEqual(abs(p.ranges[r] - 6.0625), 0.0625)

    def test_weighted_clutter_filter_with_raw_iq_and_off_bin_targets(self):
        positions = [(x * 0.06, y * 0.06) for y in range(2) for x in range(4)]
        self.processor = radar.MIMOProcessor(positions)
        p = self.processor
        mask = np.repeat(p.masks, p.receiver_count, axis=0)
        t = np.arange(p.history) * p.dt
        rng = np.random.default_rng(242)
        reference = mask * np.exp(1j * rng.uniform(-np.pi, np.pi, (p.path_count, 1)))
        reference *= 1 + 0.25 * np.exp(-2j * np.pi * p.offsets * 2 * 2.7 / radar.C)
        jitter = 0.001 * rng.normal(size=(p.history, p.path_count, 1)) + 0.003j * rng.normal(size=(p.history, p.path_count, 1))
        drift = 0.005j * rng.normal(size=(p.history, p.path_count, 1)) * p.offsets / radar.RATE
        background = reference * (1 + jitter + drift)
        p.set_range_response(reference)
        self.assertGreater(np.ptp(p.range_weights), 0.1)
        for distance in [0.3, 0.7, 1.1, 3.7, 9.7]:
            for missing in [False, True]:
                with self.subTest(distance=distance, missing=missing):
                    target = 0.03 * mask * np.exp(-2j * np.pi * p.offsets * 2 * distance / radar.C)
                    target *= np.exp(2j * np.pi * p.frequency / radar.C * p.virtual_positions[:, 0, None] * np.sin(np.deg2rad(23.7)))
                    data = background + target * np.exp(-2j * np.pi * 2 * p.frequency / radar.C * 0.437 * t)[:, None, None]
                    spectrum = np.sum(data.reshape(p.history, 2, p.receiver_count, radar.SAMPLES) * p.pilots[None, :, None], axis=1)
                    iq = np.fft.ifft(spectrum, axis=-1)
                    p.reset()
                    p.frames.extend(None if missing and i % 7 == 0 else p.channels(frame, np.ones_like(frame)) for i, frame in enumerate(iq))
                    rd, ra = p.maps()
                    r, d = np.unravel_index(rd.argmax(), rd.shape)
                    self.assertGreater(p._clutter_weights.mean(), 0.9)
                    self.assertLessEqual(abs(p.ranges[r] - distance), 0.0625 + 1e-8)
                    self.assertLessEqual(abs(p.velocities[d] - 0.437), abs(p.velocities[1] - p.velocities[0]) / 2)
                    self.assertLess(abs(p.angles[ra[r].argmax()] - 23.7), 2)
        self.assert_direct_maps_match((rd, ra))

    def test_direct_maps_with_odd_history_and_missing_captures(self):
        positions = [(x * 0.06, y * 0.06) for y in range(2) for x in range(4)]
        self.processor = radar.MIMOProcessor(positions, history=73)
        p = self.processor
        p.range_weights = 1 + 0.5 * (2 * p.offsets / radar.RATE) ** 2
        self.simulate([(0.9375, 0.37, 23.7, 0.05), (6.0625, -0.61, -31.2, 0.03)], noise=0.001)
        frames = list(p.frames)
        p.frames.clear()
        p.frames.extend(None if i % 7 == 0 else frame for i, frame in enumerate(frames))
        self.assert_direct_maps_match(p.maps())

    def fluctuating_coupling(self, targets):
        """Strong shared leakage noise near zero range, weaker thermal noise elsewhere."""
        p = self.processor
        self.simulate(targets, noise=0.0001, seed=441)
        mask = np.repeat(p.masks, p.receiver_count, axis=0)
        jitter = 1j * 0.001 * np.random.default_rng(440).normal(size=(p.history, 1, 1)) * mask
        frames = np.asarray(p.frames) + jitter
        p.frames.clear()
        p.frames.extend(frames.astype(np.complex64))
        return p.maps()

    def test_gain_and_timing_noise_do_not_create_half_metre_per_second_targets(self):
        p = self.processor
        mask = np.repeat(p.masks, p.receiver_count, axis=0)
        t = np.arange(p.history) * p.dt
        omega = 2 * np.pi * 2 * p.frequency * 0.5 / radar.C
        rng = np.random.default_rng(842)
        phase = 0.003 * np.sin(omega * t) + 0.001 * rng.normal(size=p.history)
        timing = 0.003 * np.cos(omega * t) + 0.001 * rng.normal(size=p.history)
        gain = 0.001 * rng.normal(size=p.history)
        leakage = mask * ((1 + gain[:, None, None]) * np.exp(1j * (phase[:, None, None] + timing[:, None, None] * p.offsets / radar.RATE)) - 1)
        for targets in [[], [(6, 0.5, 25, 0.0005), (9, -0.5, -30, 0.0005)]]:
            self.simulate(targets, noise=0.0001)
            frames = np.asarray(p.frames) + leakage
            p.frames.clear()
            p.frames.extend(frames.astype(np.complex64))
            maps = p.maps()
            if not targets:
                for image in p.display_maps(maps):
                    self.assertLess(np.mean(image > 12), 0.02)
            else:
                rd, ra = maps
                for distance, velocity, angle, _ in targets:
                    d = np.argmin(abs(p.velocities - velocity))
                    r = rd[:, d].argmax()
                    self.assertLessEqual(abs(p.ranges[r] - distance), 0.125)
                    self.assertLess(abs(p.angles[ra[r].argmax()] - angle), 2)
                    self.assertGreater(10 * np.log10(rd[r, d] / p.map_noise), 12)

    def visible_maps(self, maps, floor=12):
        return [np.where(power > floor, power, -200) for power in self.processor.display_maps(maps)]

    def test_weak_distant_targets_are_not_hidden_by_nearby_noise(self):
        p = self.processor
        targets = [(6, 0.5, 25, 0.0001), (9, -0.6, -30, 0.0001)]
        maps = self.fluctuating_coupling(targets)
        rd, ra = self.visible_maps(maps)
        for distance, velocity, angle, _ in targets:
            r = np.argmin(abs(p.ranges - distance))
            a = np.argmin(abs(p.angles - angle))
            d = np.argmin(abs(p.velocities - velocity))
            self.assertGreater(ra[r, a], 12)
            self.assertGreater(rd[r, -d - 1], 12)
            self.assertLessEqual(abs(p.ranges[rd[:, -d - 1].argmax()] - distance), 0.0625)

    def test_leakage_suppression_does_not_brighten_distant_sidelobes(self):
        p = self.processor
        for distance in [0.3, 0.7, 1.1, 1.6, 2]:
            with self.subTest(distance=distance):
                rd, ra = self.visible_maps(self.fluctuating_coupling([(distance, 0.5, 25, 0.0005)]))
                d = np.argmin(abs(p.velocities - 0.5))
                a = np.argmin(abs(p.angles - 25))
                self.assertLessEqual(abs(p.ranges[rd[:, -d - 1].argmax()] - distance), 0.0625)
                self.assertLessEqual(abs(p.ranges[ra[:, a].argmax()] - distance), 0.0625)

    def test_shared_coupling_noise_stays_quiet_without_targets(self):
        maps = self.fluctuating_coupling([])
        for image in self.visible_maps(maps):
            self.assertLess(np.mean(image > 12), 0.01)
        low = self.visible_maps(maps, floor=3)
        high = self.visible_maps(maps, floor=18)
        for a, b in zip(low, high):
            self.assertLessEqual(np.count_nonzero(b > -200), np.count_nonzero(a > -200))

    def test_longer_window_recovers_weak_slow_distant_motion(self):
        positions = [(x * 0.06, y * 0.06) for y in range(2) for x in range(4)]
        results = {}
        for history in (72, 144):
            self.processor = radar.MIMOProcessor(positions, history=history)
            p = self.processor
            maps = self.simulate([(9, 0.1, -30, 0.0001)], noise=0.001)
            r = np.argmin(abs(p.ranges - 9))
            d = np.argmin(abs(p.velocities - 0.1))
            power = p.display_maps(maps)[0]
            results[history] = power[r, -d - 1]
            if history == 144:
                displayed, _ = self.visible_maps(maps)
                self.assertGreater(displayed[r, -d - 1], 12)
                self.assertLessEqual(abs(p.ranges[maps[0][:, d].argmax()] - 9), 0.25)
        self.assertLess(results[72], 12)
        self.assertGreater(results[144] - results[72], 8)

    def test_longer_window_preserves_two_target_ranges_and_angles(self):
        positions = [(x * 0.06, y * 0.06) for y in range(2) for x in range(4)]
        self.processor = radar.MIMOProcessor(positions, history=144)
        self.test_two_targets_at_different_ranges_and_directions()
        self.test_slow_target_doppler_is_not_pushed_outward()

    def test_longer_window_keeps_background_quiet(self):
        positions = [(x * 0.06, y * 0.06) for y in range(2) for x in range(4)]
        self.processor = radar.MIMOProcessor(positions, history=144)
        for seed in range(3):
            maps = self.simulate(noise=0.001, drift=0.1, seed=seed)
            for image in self.visible_maps(maps):
                self.assertLess(np.mean(image > 12), 0.02)

    def test_counter_wrap_duplicates_and_late_capture(self):
        p = self.processor
        p.reset()
        frame = np.ones((p.path_count, radar.SAMPLES), np.complex64)
        start = radar.COUNTER_MODULUS - radar.INTERVAL
        self.assertTrue(p.push(frame, start))
        self.assertFalse(p.push(frame, start))
        self.assertTrue(p.push(frame, radar.INTERVAL))
        self.assertEqual(p.missing, 1)
        self.assertIsNone(p.frames[1])
        self.assertTrue(p.push(frame, 0))
        self.assertEqual(p.missing, 0)
        self.assertFalse(p.push(frame, 0))


if __name__ == "__main__":
    unittest.main()
