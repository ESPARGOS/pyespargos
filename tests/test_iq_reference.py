"""Numerical regressions for broadband synchronization and calibration."""

import unittest
from types import SimpleNamespace

import numpy as np

from espargos.iq_reference import fit, relative_lag


class BroadbandTests(unittest.TestCase):
    def test_lag_sign_and_unknown_phase(self):
        rng = np.random.default_rng(31)
        source = rng.normal(size=2048) + 1j * rng.normal(size=2048)
        reference = source[400:1424]
        for offset in (-350, -17, 0, 123, 350):
            samples = source[400 - offset : 1424 - offset] * np.exp(0.9j)
            lag, coherence = relative_lag(samples, reference)
            self.assertEqual(lag, offset)
            self.assertGreater(coherence, 0.99)

    def test_repeated_preamble_uses_measured_onset(self):
        rng = np.random.default_rng(9)
        period = rng.normal(size=64) + 1j * rng.normal(size=64)
        reference = np.tile(period, 16)
        samples = np.roll(reference, 11) * np.exp(1.2j)
        lag, confidence = relative_lag(samples, reference, expected_lag=13)
        self.assertEqual(lag, 11)
        self.assertGreater(confidence, 0.99)

    def test_threshold_crossing_is_only_a_lag_hint(self):
        rng = np.random.default_rng(72)
        source = rng.normal(size=2048) + 1j * rng.normal(size=2048)
        reference = source[400:1424]
        samples = source[383:1407] * np.exp(1.2j)
        for hint in (-18, 52):
            lag, confidence = relative_lag(samples, reference, expected_lag=hint)
            self.assertEqual(lag, 17)
            self.assertGreater(confidence, 0.99)

    def test_onset_chunks_can_differ(self):
        rng = np.random.default_rng(72)
        source = rng.normal(size=2048) + 1j * rng.normal(size=2048)
        reference = source[400:1424]
        for offset in (256, 512):
            samples = source[400 + offset : 1424 + offset] * np.exp(1.2j)
            lag, confidence = relative_lag(samples, reference, expected_lag=0)
            self.assertEqual(lag, -offset)
            self.assertGreater(confidence, 0.99)
            lag, _ = relative_lag(reference, samples, expected_lag=0)
            self.assertEqual(lag, offset)

    def test_unrelated_snapshots_rejected(self):
        rng = np.random.default_rng(31)
        with self.assertRaises(RuntimeError):
            relative_lag(rng.normal(size=1024), rng.normal(size=1024), expected_lag=0)

    def test_phase_delay_in_broadband_noise(self):
        rng = np.random.default_rng(91)
        delays = np.array([0, -30, 29, -5.4, 0.12, 1.3, -2, 6.6])
        phases = np.array([0, 1, -2, 0.2, 0.9, -1.4, 2.5, -2.8])
        n = 8192
        f = np.fft.fftfreq(n)
        source = rng.normal(size=n) + 1j * rng.normal(size=n)
        F = np.fft.fft(source) * (np.abs(f) < 0.22)
        x = np.fft.ifft(F[None] * np.exp(1j * phases[:, None] + 2j * np.pi * delays[:, None] * f), axis=-1)
        x += 0.015 * (rng.normal(size=x.shape) + 1j * rng.normal(size=x.shape))
        clusters = [SimpleNamespace(iq=x[:, i : i + 256].reshape(1, 2, 4, 256)) for i in range(256, n - 256, 256)]
        tau, phi, quality = fit(clusters, 0)
        np.testing.assert_allclose(tau, delays, atol=0.025)
        np.testing.assert_allclose(np.angle(np.exp(1j * (phi - phases))), 0, atol=0.025)
        self.assertGreater(quality.min(), 0.99)

    def test_independent_receivers_rejected(self):
        rng = np.random.default_rng(15)
        clusters = [SimpleNamespace(iq=rng.normal(size=(1, 2, 4, 256)) + 1j * rng.normal(size=(1, 2, 4, 256))) for _ in range(20)]
        with self.assertRaises(RuntimeError):
            fit(clusters, 0)

    def test_phase_slope_across_missing_frequency_bins(self):
        from unittest.mock import patch

        rng = np.random.default_rng(14)
        frequencies = np.fft.fftfreq(256)
        active = np.isin(np.arange(256), np.array([-40, -32, -24, -16, 16, 24, 32, 40]) % 256)
        delays = np.array([0, -12, 14, -5.4, 0.12, 1.3, -2, 6.6])
        phases = rng.uniform(-np.pi, np.pi, 8)
        phases -= phases[0]
        clusters = []
        for _ in range(20):
            spectrum = (rng.normal(size=256) + 1j * rng.normal(size=256)) * active
            samples = np.fft.ifft(spectrum * np.exp(1j * phases[:, None] + 2j * np.pi * delays[:, None] * frequencies))
            clusters.append(SimpleNamespace(iq=samples))
        # A rectangular window preserves the deliberately empty bins, so
        # this checks slope recovery across gaps rather than window leakage.
        with patch("numpy.hanning", return_value=np.ones(256)):
            tau, phi, quality = fit(clusters, 0)
        np.testing.assert_allclose(tau, delays, atol=1e-8)
        np.testing.assert_allclose(np.angle(np.exp(1j * (phi - phases))), 0, atol=1e-8)
        self.assertGreater(quality.min(), 0.999)


class ReferenceSessionTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        self.wait = patch("espargos.iq_tx_control._wait").start()
        self.addCleanup(patch.stopall)

    def test_delay_transfer_removes_index_wraps(self):
        from espargos.iq_reference import transfer_delay

        old = np.array([100000, 200000, 300000], dtype=np.int64)
        new = old + np.array([2, 22, -8]) + np.array([0, 53687091200, -2 * 53687091200])
        np.testing.assert_allclose(transfer_delay([0, 1e-9, -3e-9], old, new, 0), [0, 21e-9, -13e-9], atol=1e-15)

    def test_failed_session_restores_settings_and_releases_lock(self):
        import copy
        import threading
        from espargos.iq_reference import reference_session

        saved = dict(
            mode="iq",
            adc_decimation=10,
            rf_switch=2,
            trigger=dict(mode="signal", threshold=64, antennas=list(range(8)), holdoff_ms=300, capture_chunks=56, quiet_threshold=0),
            receivers=[dict(antid=a, rf_freq_hz=2437000000, gain_mode="auto", rx_gain=60) for a in range(8)],
        )
        writes = []
        board = SimpleNamespace(wifi_rx=SimpleNamespace(get_config=lambda: {"calib-source": 0}), iq=SimpleNamespace(get_config=lambda: copy.deepcopy(saved), set_reference=lambda: writes.append("off"), set_config=lambda c: writes.append(c)))
        pool = SimpleNamespace(boards=[board], master_board=board, _reference_lock=threading.Lock(), _owner=SimpleNamespace(start_processing=lambda: None), apply_config=lambda c: writes.append(c), _calibrating=False)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with reference_session(pool) as (center, fs):
                self.assertEqual(fs, 4e6)
                self.assertTrue(pool._calibrating)
                raise RuntimeError("injected")
        self.assertFalse(pool._calibrating)
        self.assertFalse(pool._reference_lock.locked())
        for key in ("receivers", "adc_decimation", "rf_switch", "trigger"):
            self.assertEqual(writes[-1][key], saved[key])
        self.assertEqual(writes[-2], "off")

    def test_reference_gain_follows_routing_and_allows_override(self):
        import threading
        from espargos.iq_reference import reference_session

        saved = dict(adc_decimation=1, rf_switch=3, trigger={"mode": "interval"}, receivers=[dict(antid=a, rf_freq_hz=2437000000) for a in range(8)])
        for source, override, expected in [(0, None, 30), (1, None, 42), (2, None, 42), (1, 35, 35)]:
            writes = []
            board = SimpleNamespace(wifi_rx=SimpleNamespace(get_config=lambda: {"calib-source": source}), iq=SimpleNamespace(get_config=lambda: saved, set_reference=lambda: None, set_config=lambda c: None))
            pool = SimpleNamespace(boards=[board], master_board=board, _reference_lock=threading.Lock(), _owner=SimpleNamespace(start_processing=lambda: None), apply_config=writes.append, _calibrating=False)
            with reference_session(pool, rx_gain=override):
                self.assertEqual(writes[0]["rx_gain"], expected)

    def test_grid_and_rate_transitions_cannot_collide_in_cache(self):
        from espargos.iq_receiver import IQReceiver

        pool = IQReceiver.__new__(IQReceiver)
        keys = []
        for sequence, rate in [(7, 80_000_000), (7, 40_000_000), (8, 80_000_000)]:
            message = SimpleNamespace(payload=SimpleNamespace(sync_info=sequence, sample_rate_hz=rate))
            keys.append(pool._resolve_cluster_key("chunks", 123, {}, 0, message))
        self.assertEqual(len(set(keys)), 3)

    def test_out_of_order_sensor_packets_across_index_wrap(self):
        import threading
        from espargos.iq_receiver import IQReceiver

        pool = IQReceiver.__new__(IQReceiver)
        pool._unwrap_lock = threading.Lock()
        pool._unwrap_epoch = 0
        pool._unwrap_last = None
        wrap = 1 << 24
        raw = [wrap - 2, 0, wrap - 1, 1, 0, 2]
        indices = [pool._get_cluster_key(0, SimpleNamespace(payload=SimpleNamespace(is_signal_capture=False, source_chunk_index=i))) for i in raw]
        self.assertEqual(indices, [wrap - 2, wrap, wrap - 1, wrap + 1, wrap, wrap + 2])

    def test_iq_api_version_compatibility(self):
        from espargos.board import EspargosAPIVersionError
        from espargos.board_iq import IQCapability

        IQCapability(SimpleNamespace(api_version=(4, 0)))
        for major in (3, 5):
            with self.subTest(major=major):
                with self.assertRaisesRegex(EspargosAPIVersionError, "major 4"):
                    IQCapability(SimpleNamespace(api_version=(major, 0)))


if __name__ == "__main__":
    unittest.main()
