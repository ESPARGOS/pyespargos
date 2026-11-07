import unittest

import numpy as np

from espargos.iq_tx_reciprocity_fit import (
    DisconnectedLinks,
    _fit_link,
    additional_reference,
    fit_links,
    validate_links,
)


class ReciprocalFitTests(unittest.TestCase):
    def test_sparse_noisy_pilot_comb(self):
        # Sparse bins from the failed kit capture. Adjacent-tone estimates can
        # amplify noise enough to choose the wrong phase unwrap.
        bins = np.array([-191, -183, -175, -111, -95, -79, 87])
        frequencies = bins * 80e6 / 1024
        rng = np.random.default_rng(4)
        for delay in (-25.5, -0.6, 2.15, 25.5):
            phase = 1.1
            noise = rng.normal(0, 0.1, len(bins))
            z = np.exp(1j * (phase + 2 * np.pi * frequencies / 80e6 * delay + noise))
            fitted_phase, fitted_delay, rms = _fit_link(frequencies, z, 80e6 / 1024)
            self.assertLess(abs(np.angle(np.exp(1j * (fitted_phase - phase)))), 0.15)
            self.assertLess(abs(fitted_delay - delay), 0.15)
            self.assertLess(rms, 0.2)

    def test_sparse_comb_does_not_select_distant_delay_alias(self):
        bins = np.array([-167, -159, -135, -127, -111, -95, 167])
        f = bins * 80e6 / 1024
        rng = np.random.default_rng(7)
        for _ in range(100):
            delay = rng.uniform(-4, 4)
            phase = rng.uniform(-np.pi, np.pi)
            z = np.exp(1j * (phase + 2 * np.pi * f / 80e6 * delay + rng.normal(0, 0.1, len(bins))))
            _, fitted, _ = _fit_link(f, z, 80e6 / 1024)
            self.assertLess(abs(fitted - delay), 0.2)

    def test_one_narrow_interferer_does_not_break_phase_unwrap(self):
        bins = np.array([k for k in range(-192, 193) if abs(k) >= 8 and abs(k) % 8 == 6])
        f = bins * 80e6 / 1024
        rng = np.random.default_rng(11)
        for delay in (-25.5, -0.7, 2.4, 25.5):
            phase = 2.8
            z = np.exp(1j * (phase + 2 * np.pi * f / 80e6 * delay + rng.normal(0, 0.04, len(f))))
            # Near 2440 MHz with a 2437 MHz carrier, as in the kit capture.
            z[np.argmin(abs(f - 3e6))] *= np.exp(2.9j)
            p, d, rms = _fit_link(f, z, 80e6 / 1024)
            self.assertLess(abs(np.angle(np.exp(1j * (p - phase)))), 0.03)
            self.assertLess(abs(d - delay), 0.03)
            self.assertLess(rms, 0.08)

    def test_broad_corruption_is_not_hidden_by_outlier_rejection(self):
        bins = np.arange(-190, 191, 8)
        f = bins * 80e6 / 1024
        z = np.exp(1j * (0.5 + 2 * np.pi * f / 80e6))
        z[::4] *= np.exp(2j)
        _, _, rms = _fit_link(f, z, 80e6 / 1024)
        self.assertGreater(rms, 0.2)

    def test_reciprocal_paths_cancel_and_disconnection_is_rejected(self):
        rng = np.random.default_rng(42)
        n = 32
        bins = np.arange(-192, 193)
        frequencies = bins * 80e6 / 1024
        phase = rng.uniform(-np.pi, np.pi, n)
        delay = rng.uniform(-4, 4, n)
        # Arbitrary reciprocal propagation, equally applicable to REF and OTA.
        path = rng.normal(size=(len(bins), n, n)) + 1j * rng.normal(size=(len(bins), n, n))
        path += path.transpose(0, 2, 1).copy()
        response = path * np.exp(1j * phase[None, None, :] - 2j * np.pi * frequencies[:, None, None] / 80e6 * delay[None, None, :])
        valid = np.zeros(response.shape, bool)
        for sensor in range(n):
            for hub in (0, 8):
                if sensor != hub:
                    selected = (abs(bins) % 8 == sensor % 8) & (abs(frequencies) > 1e6)
                    valid[selected, sensor, hub] = True
                    valid[selected, hub, sensor] = True
        p, d, _ = fit_links(frequencies, response, valid)
        np.testing.assert_allclose(np.angle(np.exp(1j * (np.deg2rad(p) + phase - phase[0]))), 0, atol=1e-10)
        np.testing.assert_allclose(d + delay - delay[0], 0, atol=1e-10)
        valid[:, 31, :] = False
        valid[:, :, 31] = False
        with self.assertRaisesRegex(RuntimeError, "do not connect"):
            fit_links(frequencies, response, valid)

    def test_independent_validation_detects_phase_and_delay_changes(self):
        f = np.arange(-190, 191) * 80e6 / 1024
        phase = np.array([0.3, -2.8, 2.9, -0.2])
        delay = np.array([0.1, -0.5, 0.8, 1.1])
        z = np.exp(1j * phase - 2j * np.pi * f[:, None] / 80e6 * delay)
        response = np.broadcast_to(z[:, None, :], (len(f), 4, 4)).copy()
        valid = np.ones(response.shape, bool)
        p, d, _ = fit_links(f, response, valid)
        q = validate_links(f, response, valid, p, d)
        self.assertLess(q["phase_residual_deg"], 1e-9)
        response[:, :, 2] *= np.exp(0.5j - 2j * np.pi * f[:, None] / 80e6 * 0.7)
        q = validate_links(f, response, valid, p, d)
        self.assertGreater(q["phase_residual_deg"], 5)
        self.assertGreater(q["delay_residual_samples"], 0.3)

    def test_additional_reference_repairs_disconnected_pilot_graph(self):
        f = np.arange(-190, 191) * 80e6 / 1024
        n = 8
        response = np.ones((len(f), n, n), complex)
        valid = np.zeros(response.shape, bool)
        # First two dense pilots reach only sensors 0..3. Other transmitters
        # use disjoint combs, but sensor 3 can hear the remaining sensors.
        for tx in range(n):
            good = np.ones(len(f), bool) if tx < 2 else np.arange(len(f)) % 8 == tx
            for rx in range(n):
                if rx != tx and (tx >= 2 or rx < 4):
                    valid[good, rx, tx] = True
        with self.assertRaises(DisconnectedLinks) as caught:
            fit_links(f, response, valid)
        candidate = additional_reference(caught.exception.components, [0, 1], valid)
        self.assertNotIn(candidate, (0, 1))
        valid[:, :, candidate] = True
        p, d, q = fit_links(f, response, valid)
        self.assertGreaterEqual(q["links"], n - 1)
        np.testing.assert_allclose(p, 0, atol=1e-10)
        np.testing.assert_allclose(d, 0, atol=1e-10)
        self.assertIsNone(additional_reference([list(range(n))], range(n), valid))

    def test_cycle_inconsistency_is_visible_in_quality(self):
        f = np.arange(-190, 191) * 80e6 / 1024
        response = np.ones((len(f), 4, 4), complex)
        response[:, 0, 1] *= np.exp(1j)
        _, _, quality = fit_links(f, response, np.ones(response.shape, bool))
        self.assertGreater(quality["phase_residual_deg"], 5)

    def test_single_sensor_needs_no_reciprocal_link(self):
        f = np.arange(-190, 191) * 80e6 / 1024
        response = np.zeros((len(f), 1, 1), complex)
        valid = np.zeros(response.shape, bool)
        p, d, quality = fit_links(f, response, valid)
        self.assertEqual(quality, validate_links(f, response, valid, p, d))
        self.assertEqual(quality["links"], 0)


if __name__ == "__main__":
    unittest.main()
