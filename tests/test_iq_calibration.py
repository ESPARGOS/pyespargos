"""Calibration lookup and selection invariants, without hardware."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from threading import RLock

import numpy as np

from espargos.calibration import Antenna, AntennaCorrection, Calibration, CalibrationGroup, CalibrationState
from espargos._iq_calibration.scope import CalibrationScope
from espargos.iq_tx_rx_delay import TxRxDelayCalibration


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.states = [CalibrationState(Antenna("array", a), "rx", 2.4e9, 80e6, 1, 10) for a in range(2)]
        self.result = Calibration((CalibrationGroup(tuple(AntennaCorrection(s, a * np.pi / 2, a * 25e-9) for a, s in enumerate(self.states))),))

    def test_response_orders_rows_and_uses_rf_frequencies(self):
        actual = self.result.response(self.states[::-1], [2.4e9, 2.41e9])
        np.testing.assert_allclose(actual, [[1j, 1], [1, 1]], atol=1e-14)
        self.assertEqual(self.result.response([], [2.4e9]).shape, (0, 1))
        with self.assertRaises(KeyError):
            self.result.response([replace(self.states[0], epoch=11)], [2.4e9])
        with self.assertRaises(ValueError):
            self.result.response(self.states, [[2.4e9]])

    def test_delay_response_leaves_carrier_phase_unchanged(self):
        tx = replace(self.states[0], direction="tx")
        calibration = self.result.merge(Calibration((CalibrationGroup((AntennaCorrection(tx, 0, 0),)),)))
        delay = TxRxDelayCalibration(calibration, {tx.antenna: 25e-9})
        np.testing.assert_allclose(delay.response(tx.antenna, [2.4e9, 2.41e9]), [1, 1j], atol=1e-14)

    def test_partial_replacement_is_rejected(self):
        updated = Calibration((CalibrationGroup((AntennaCorrection(self.states[0], 0.2, 1e-9),)),))
        with self.assertRaisesRegex(ValueError, "complete calibration group"):
            self.result.merge(updated)
        with self.assertRaisesRegex(ValueError, "array/1"):
            self.result._check_replacement([(self.states[0].antenna, "rx", 2.4e9)])
        self.assertEqual(len(self.result.select(antennas=[self.states[0].antenna]).groups[0].corrections), 1)

    def test_complete_replacement_and_extension(self):
        extra = replace(self.states[0], antenna=Antenna("array", 2))
        updated = Calibration((CalibrationGroup(tuple(AntennaCorrection(s, 0, 0) for s in [*self.states, extra])),))
        self.assertEqual(self.result.merge(updated).groups, updated.groups)

    def test_independent_groups_require_separate_processing(self):
        separate = Calibration(tuple(CalibrationGroup((c,)) for c in self.result.groups[0].corrections))
        with self.assertRaisesRegex(ValueError, "independent calibration references"):
            separate.response(self.states, [2.4e9])
        self.assertEqual(separate.response(self.states[:1], [2.4e9]).shape, (1, 1))
        # Measuring the full union replaces both independent groups.
        self.assertEqual(separate.merge(self.result).groups, self.result.groups)

    def test_remeasurement_invalidates_dependent_timing(self):
        tx = replace(self.states[0], direction="tx")
        calibrated = self.result.merge(Calibration((CalibrationGroup((AntennaCorrection(tx, 0, 0),)),)))
        timing = TxRxDelayCalibration(calibrated, {tx.antenna: 1e-9})
        newer = Calibration((CalibrationGroup(self.result.groups[0].corrections),))
        with self.assertRaisesRegex(ValueError, "original calibration groups"):
            timing.for_calibration(calibrated.merge(newer))

    def test_pool_rejects_overlap_before_acquisition(self):
        from espargos.pool import AntennaSelection

        board = SimpleNamespace(host="array", api_version=(4, 4), iq=SimpleNamespace(get_config=lambda: dict(modes=["iq_rx"] * 8, receivers=[dict(rf_freq_hz=2.4e9)] * 8)))
        owner = SimpleNamespace(_lock=RLock(), open=Mock(), _refresh_receivers=Mock(), boards=[board], calibrations=self.result)
        with patch("espargos.pool._rpc", return_value={}) as rpc:
            with self.assertRaisesRegex(ValueError, "complete calibration group"):
                AntennaSelection(owner, [self.states[0].antenna]).calibrate()
            self.assertEqual([call.args[1] for call in rpc.call_args_list], ["get_iq_tx"])
        self.assertIs(owner.calibrations, self.result)

    def test_geometric_beam_requires_common_tx_reference(self):
        from espargos.steering import Beamspace
        from espargos.tx_signal import TXSignals, TXSignal, _SignalSettings
        from espargos.waveforms import CW

        sensors = [dict(antid=a, applied_mode="iq_tx", enabled=True, frequency_hz=2.4e9, boot_id=1, origin_ns=10, samples=256) for a in range(8)]
        states = [CalibrationState.from_tx_status("array", s) for s in sensors[:2]]
        separate = Calibration(tuple(CalibrationGroup((AntennaCorrection(s, 0, 0),)) for s in states))
        tx = SimpleNamespace(owner=SimpleNamespace(calibrations=separate), boards=[SimpleNamespace(host="array")], states=[dict(sensors=sensors)])
        signals = TXSignals(tx)
        signals._items = [TXSignal(signals, _SignalSettings(CW(), 0.1, tuple(s.antenna for s in states), Beamspace()))]
        with self.assertRaisesRegex(ValueError, "independent calibration references"):
            signals._check_beam_calibration()
        tx.owner.calibrations = Calibration((CalibrationGroup(tuple(g.corrections[0] for g in separate.groups)),))
        signals._check_beam_calibration()
        tx.owner.calibrations = Calibration()
        signals._check_beam_calibration(preparing=True)
        with self.assertRaises(KeyError):
            signals._check_beam_calibration()


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.writes = [[], []]
        self.configs = [dict(modes=["iq_rx"] * 8, coarse_synced=True, adc_decimation=1) for _ in range(2)]
        boards = [SimpleNamespace(host=f"board{i}", revision=SimpleNamespace(antenna_id_to_row_col=lambda a: divmod(a, 4)), iq=SimpleNamespace(get_config=lambda i=i: self.configs[i], set_config=self.writes[i].append)) for i in range(2)]
        self.scope = CalibrationScope(SimpleNamespace(boards=boards), [Antenna("board1", a) for a in [2, 5]])

    def test_settings_and_modes_only_target_selected_sensors(self):
        self.configs[1]["modes"][0] = "iq_tx"
        self.scope.apply_rx(dict(mode="iq", rf_freq_hz=2.4e9, rx_gain=30))
        self.assertEqual(self.writes[0], [])
        payload = self.writes[1][0]
        self.assertEqual([r["antid"] for r in payload["receivers"]], [2, 5])
        self.assertEqual(payload["modes"][0], "iq_tx")
        self.assertEqual(self.scope.mask.sum(), 2)

    def test_subgroup_requires_existing_full_rate_grid(self):
        self.assertTrue(self.scope.check_sync(self.configs))
        self.configs[1]["coarse_synced"] = False
        with self.assertRaisesRegex(ValueError, "existing synchronized grid"):
            self.scope.check_sync(self.configs)
        self.configs[1].update(coarse_synced=True, adc_decimation=2)
        with self.assertRaisesRegex(ValueError, "80 MSa/s"):
            self.scope.check_sync(self.configs)
        self.assertEqual(self.writes, [[], []])


if __name__ == "__main__":
    unittest.main()
