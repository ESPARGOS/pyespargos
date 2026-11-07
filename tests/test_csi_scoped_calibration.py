import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
import numpy as np
from espargos.calibration import Antenna, Calibration, CalibrationGroup, AntennaCorrection, CalibrationState
from espargos.csi_scoped_calibration import channels, estimate, apply_csi, states, calibrate
from espargos.wifi_transceiver import CalibrationError
from espargos import csi_processing


class ScopedCSITests(unittest.TestCase):
    def test_actual_channels(self):
        cfg = {"channel-primary": 6, "channel-secondary": 0}
        override = {"override_active": True, "channel-primary": [1] * 4 + [11] * 4, "channel-secondary": [0] * 8}
        self.assertEqual(channels(cfg, override), [(1, 0)] * 4 + [(11, 0)] * 4)
        override["override_active"] = False
        self.assertEqual(channels(cfg, override), [(6, 0)] * 8)

    def test_subset_estimation(self):
        f, valid = csi_processing.get_csi_sto_correction_frequencies("lltf", 0)
        positions = [(0, 0, 0), (0, 1, 3)]
        times = np.zeros((1, 2, 4))
        times[0, 1, 3] = 100e-9
        delays = np.array([0, 75e-9])
        phases = np.array([0, 0.9])
        clusters = []
        for common in np.arange(8) * 0.3:
            csi = np.full((1, 2, 4, len(f)), np.nan + 1j * np.nan)
            completion = np.zeros((1, 2, 4), bool)
            for p, d, phi in zip(positions, delays, phases):
                csi[p] = np.exp(1j * (phi + common) - 2j * np.pi * f * d)
                completion[p] = True
            clusters.append(SimpleNamespace(primary_channel=6, secondary_channel_relative=0, completion=completion, sensor_timestamps=times, has_ht40ltf=False, has_ht20ltf=False, has_lltf=True, deserialize_csi_lltf=lambda csi=csi: csi))
        d, phi = estimate(clusters, positions, 6, 0)
        np.testing.assert_allclose(d, delays, atol=1e-12)
        np.testing.assert_allclose(phi, phases, atol=1e-6)
        with self.assertRaises(CalibrationError):
            estimate(clusters, positions, 11, 0)
        with self.assertRaises(CalibrationError):
            estimate(clusters[:4], positions, 6, 0)

    def test_apply_and_scope(self):
        state = CalibrationState(Antenna("a", 2), "rx", 2437e6, 20e6, 3, 4, (("mode", "wifi"), ("primary", 6), ("secondary", 0)))
        correction = AntennaCorrection(state, 0.7, 12e-9)
        result = Calibration((CalibrationGroup((correction,)),))
        for fmt in ["lltf", "ht20", "he20"]:
            from espargos.csi_scoped_calibration import frequencies

            f = frequencies(state, fmt)
            raw = 1 / correction.response(f)
            np.testing.assert_allclose(apply_csi(result, state, raw, fmt), 1, atol=1e-12)
        with self.assertRaises(ValueError):
            apply_csi(result, state, np.ones(1))
        with self.assertRaises(ValueError):
            apply_csi(result, state, np.ones(1), "ht40")
        from dataclasses import replace

        with self.assertRaises(KeyError):
            apply_csi(result, replace(state, epoch=5), np.ones(len(csi_processing.get_frequencies_lltf(6))))

    def fake_pool(self):
        b = Mock()
        b.host = "a"
        b.revision.antenna_id_to_row_col = lambda a: divmod(a, 4)
        config = {"channel-primary": 6, "channel-secondary": 0}
        override = {"override_active": True, "channel-primary": [1] * 4 + [11] * 4, "channel-secondary": [0] * 8}
        b.wifi_rx.get_config.return_value = config.copy()
        b.wifi_rx.get_channel_overrides.return_value = override.copy()
        b.wifi_rx.get_rf_switch.return_value = 2
        b.wifi_rx.get_mac_filter.return_value = {"enable": True, "mac": "x"}
        b.control.get_json.return_value = {"sensors": [{"applied_mode": "wifi", "boot_id": 5} for _ in range(8)]}
        p = SimpleNamespace(
            boards=[b], _reference_generator_boards=[], _association_epoch=0, _wifi_calibration_epoch=0, _wifi_calibration_epochs={Antenna("a", a): 0 for a in range(8)}, _gain_phase_enabled=True, _owner=SimpleNamespace(calibrations=Calibration())
        )

        def invalidate(*a, **kw):
            p._association_epoch += 1
            if kw.get("invalidate_scoped", True):
                p._wifi_calibration_epoch += 1
                for a in kw.get("antennas", p._wifi_calibration_epochs):
                    p._wifi_calibration_epochs[a] = p._wifi_calibration_epoch

        p._invalidate_association_timebase = invalidate
        p._clear_cluster_cache = Mock()
        p._get_cluster_cache_snapshot = Mock(return_value=[])
        p._run = Mock()
        return p, b

    def test_state_changes_invalidate(self):
        p, b = self.fake_pool()
        before = states(p)
        self.assertEqual(before, states(p))
        b.wifi_rx.get_channel_overrides.return_value["channel-primary"][0] = 3
        self.assertNotEqual(before, states(p))
        self.assertEqual(p._association_epoch, 1)
        b.control.get_json.return_value["sensors"][1]["boot_id"] = 6
        states(p)
        self.assertEqual(p._association_epoch, 2)

    def test_retuning_preserves_unrelated_antenna_states(self):
        p, b = self.fake_pool()
        before = states(p)
        b.wifi_rx.get_channel_overrides.return_value["channel-primary"][0] = 3
        after = states(p)
        self.assertNotEqual(before[Antenna("a", 0)], after[Antenna("a", 0)])
        for a in range(1, 8):
            self.assertEqual(before[Antenna("a", a)], after[Antenna("a", a)])
        b.wifi_rx.get_channel_overrides.return_value["channel-primary"][0] = 1
        restored = states(p)
        self.assertNotEqual(before[Antenna("a", 0)], restored[Antenna("a", 0)])

    def test_mode_round_trip_invalidates_only_that_sensor(self):
        p, b = self.fake_pool()
        before = states(p)
        b.control.get_json.return_value["sensors"][2]["applied_mode"] = "iq_rx"
        self.assertNotIn(Antenna("a", 2), states(p))
        b.control.get_json.return_value["sensors"][2]["applied_mode"] = "wifi"
        after = states(p)
        self.assertNotEqual(before[Antenna("a", 2)], after[Antenna("a", 2)])
        for a in range(8):
            if a != 2:
                self.assertEqual(before[Antenna("a", a)], after[Antenna("a", a)])

    def test_groups_and_restore(self):
        p, b = self.fake_pool()
        selected = [Antenna("a", a) for a in [0, 2, 5]]
        with patch("espargos.csi_scoped_calibration.compute_reference_path_delays", return_value=np.zeros((1, 2, 4))), patch("espargos.csi_scoped_calibration.estimate", side_effect=lambda c, pos, p, s: (np.zeros(len(pos)), np.zeros(len(pos)))):
            result = calibrate(p, antennas=selected, duration=0.001)
        self.assertEqual([len(g.corrections) for g in result.groups], [2, 1])
        self.assertTrue(result.covers(states(p)[a] for a in selected))
        self.assertFalse(result.covers([states(p)[Antenna("a", 1)]]))
        b.wifi_rx.set_rf_switch.assert_called_with(2)
        b.wifi_rx.set_reference_config.assert_called_with({"channel-primary": 6, "channel-secondary": 0})
        b.wifi_rx.set_channel_overrides.assert_not_called()

    def test_failure_restores_without_publishing(self):
        p, b = self.fake_pool()
        with patch("espargos.csi_scoped_calibration.compute_reference_path_delays", return_value=np.zeros((1, 2, 4))), patch("espargos.csi_scoped_calibration.estimate", side_effect=CalibrationError("missing")):
            with self.assertRaises(CalibrationError):
                calibrate(p, duration=0.001)
        self.assertFalse(p._owner.calibrations.groups)
        b.wifi_rx.set_rf_switch.assert_called_with(2)
        b.wifi_rx.set_mac_filter.assert_called_with({"enable": True, "mac": "x"})
        b.wifi_rx.set_reference_config.assert_called_with({"channel-primary": 6, "channel-secondary": 0})


class ClusterApplicationTests(unittest.TestCase):
    def test_mask_and_channel_checks(self):
        from espargos.csi_scoped_calibration import apply_cluster

        state = CalibrationState(Antenna("a", 2), "rx", 2437e6, 20e6, 3, 4, (("mode", "wifi"), ("primary", 6), ("secondary", 0)))
        correction = AntennaCorrection(state, 0.4, 1e-9)
        result = Calibration((CalibrationGroup((correction,)),))
        board = SimpleNamespace(host="a", revision=SimpleNamespace(antenna_id_to_row_col=lambda a: divmod(a, 4)))
        pool = SimpleNamespace(boards=[board])
        raw = np.ones((1, 2, 4, len(csi_processing.get_frequencies_lltf(6))), complex)
        cluster = SimpleNamespace(primary_channel=6, secondary_channel_relative=0, completion=np.ones((1, 2, 4), bool), has_lltf=True, deserialize_csi_lltf=lambda: raw)
        output = apply_cluster(pool, cluster, "lltf", result, {state.antenna: state})
        self.assertEqual(np.isfinite(output).all(axis=-1).sum(), 1)
        np.testing.assert_allclose(output[0, 0, 2], apply_csi(result, state, raw[0, 0, 2]))
        from dataclasses import replace

        with self.assertRaises(KeyError):
            apply_cluster(pool, cluster, "lltf", result, {state.antenna: replace(state, epoch=5)})
        cluster.primary_channel = 11
        with self.assertRaises(KeyError):
            apply_cluster(pool, cluster, "lltf", result, {state.antenna: state})

    def test_zero_reference_rejected(self):
        c = SimpleNamespace(
            primary_channel=6,
            secondary_channel_relative=0,
            completion=np.ones((1, 2, 4), bool),
            sensor_timestamps=np.zeros((1, 2, 4)),
            has_ht40ltf=False,
            has_ht20ltf=False,
            has_lltf=True,
            deserialize_csi_lltf=lambda: np.zeros((1, 2, 4, len(csi_processing.get_frequencies_lltf(6))), complex),
        )
        with self.assertRaises(CalibrationError):
            estimate([c] * 5, [(0, 0, 0)], 6, 0)


if __name__ == "__main__":
    unittest.main()
