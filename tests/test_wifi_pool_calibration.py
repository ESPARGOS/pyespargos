"""Pool WiFi calibration preserves CSI correction and timestamp association."""

from dataclasses import replace
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from espargos.calibration import Antenna, Calibration, CalibrationState
from espargos.csi_calibration import CSICalibration
from espargos.csi_scoped_calibration import calibrate_full_array, states
from espargos.pool import AntennaSelection
from espargos.wifi_transceiver import CalibrationError


class WiFiPoolCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.boards = []
        for i in range(2):
            board = Mock(host=f"board{i}", api_version=(4, 4))
            board.revision.antenna_id_to_row_col.side_effect = lambda a: (a // 4, a % 4)
            board.revision.calib_trace_delays = np.arange(8).reshape(2, 4) * 0.1e-9
            board.wifi_rx.get_config.return_value = {"channel-primary": 5, "channel-secondary": 0}
            board.iq.get_config.return_value = {"modes": ["wifi"] * 8}
            self.boards.append(board)
        shape = (2, 2, 4)
        self.model = CSICalibration(self.boards, 5, 0, np.arange(16).reshape(shape) * 1e-8, np.arange(16).reshape(shape) * 0.2, board_cable_lengths=[0, 1.5], board_cable_vfs=[0.76, 0.76])
        self.before = {Antenna(b.host, a): CalibrationState(Antenna(b.host, a), "rx", 2432e6, 20e6, 10, 1, (("mode", "wifi"), ("primary", 5), ("secondary", 0), ("gain_phase", True))) for b in self.boards for a in range(8)}
        self.after = {a: replace(s, epoch=2) for a, s in self.before.items()}
        self.receiver = Mock(boards=self.boards, _reference_generator_boards=[], calibration=self.model)

    def test_one_measurement_preserves_array_model_and_scoped_response(self):
        for per_board in (True, False):
            with self.subTest(per_board=per_board), patch("espargos.csi_scoped_calibration.states", side_effect=[self.before, self.after]):
                result = calibrate_full_array(self.receiver, per_board=per_board, duration=0.5)
                self.assertEqual(len(result.groups), 2 if per_board else 1)
                frequencies = np.array([2425e6, 2432e6, 2439e6])
                expected = self.model.phase_time_correction(frequencies).reshape(16, -1)
                actual = np.stack([result.correction(s).response(frequencies) for s in self.after.values()])
                np.testing.assert_allclose(actual, expected, atol=1e-7)
                self.assertIs(self.receiver.calibration, self.model)
                self.receiver.calibrate.assert_called_once()
                self.receiver.calibrate.reset_mock()
                self.receiver._invalidate_association_timebase.assert_not_called()

    def test_changed_boot_rejects_result(self):
        antenna = next(iter(self.after))
        self.after[antenna] = replace(self.after[antenna], boot_id=11)
        with patch("espargos.csi_scoped_calibration.states", side_effect=[self.before, self.after]):
            with self.assertRaises(CalibrationError):
                calibrate_full_array(self.receiver, per_board=False, duration=0.5)
        self.receiver._invalidate_association_timebase.assert_called_once()

    def test_reference_channel_restored_after_measurement_failure(self):
        self.boards[0].wifi_rx.get_config.return_value = {"channel-primary": 11, "channel-secondary": 0}
        self.receiver.calibrate.side_effect = RuntimeError("capture failed")
        with patch("espargos.csi_scoped_calibration.states", return_value=self.before):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                calibrate_full_array(self.receiver, per_board=False, duration=0.5)
        self.assertEqual([c.args[0] for c in self.boards[0].wifi_rx.set_reference_config.call_args_list], [{"channel-primary": 5, "channel-secondary": 0}, {"channel-primary": 11, "channel-secondary": 0}])
        self.receiver._invalidate_association_timebase.assert_called_once()

    def test_pool_uses_full_array_path_only_for_complete_uniform_channel(self):
        owner = SimpleNamespace(
            _lock=RLock(), open=Mock(), boards=self.boards, _refresh_receivers=Mock(), calibrations=Calibration(), antennas=list(self.before), wifi=self.receiver, _geometry=SimpleNamespace(cables=None, velocity_factors=None), _tx=None
        )
        self.receiver.calibrate_channels.return_value = Calibration()
        for selection in (owner.antennas, owner.antennas[:3]):
            with patch("espargos.csi_scoped_calibration.states", return_value=self.before), patch("espargos.csi_scoped_calibration.calibrate_full_array", return_value=Calibration()) as full:
                AntennaSelection(owner, selection).calibrate()
                self.assertEqual(full.call_count, int(selection == owner.antennas))
        self.receiver.calibrate_channels.assert_called_once()
        differing = dict(self.before)
        a = owner.antennas[0]
        differing[a] = replace(differing[a], frequency_hz=2437e6, settings=(("mode", "wifi"), ("primary", 6), ("secondary", 0)))
        with patch("espargos.csi_scoped_calibration.states", return_value=differing), patch("espargos.csi_scoped_calibration.calibrate_full_array") as full:
            AntennaSelection(owner, owner.antennas).calibrate()
            full.assert_not_called()

    def test_api3_states_do_not_require_iq_or_channel_override_endpoints(self):
        board = self.boards[0]
        board.api_version = (3, 0)
        receiver = SimpleNamespace(boards=[board], _gain_phase_enabled=True, _wifi_calibration_epochs={Antenna(board.host, a): 1 for a in range(8)})
        current = states(receiver)
        self.assertEqual(len(current), 8)
        self.assertTrue(all(s.boot_id == 0 for s in current.values()))
        board.control.get_json.assert_not_called()
        board.wifi_rx.get_channel_overrides.assert_not_called()
