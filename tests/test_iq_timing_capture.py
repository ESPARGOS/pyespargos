"""Full-rate timing probes must preserve the application's slower RX grid."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import numpy as np
from espargos.calibration import Antenna, AntennaCorrection, Calibration, CalibrationGroup, CalibrationState
from espargos._iq_calibration.pilots import pilots
from espargos._iq_calibration.delay import measure


class TimingCaptureTests(TestCase):
    def setUp(self):
        self.tx, self.rx = Antenna("array", 0), Antenna("array", 1)
        self.config = dict(modes=["iq_tx", "iq_rx"] + ["off"] * 6, adc_decimation=6, rf_switch=3, receivers=[], trigger={"mode": "accumulate"}, config_generation=1)
        self.board = SimpleNamespace(host="array", iq=SimpleNamespace(get_config=Mock(return_value=self.config), set_config=Mock()))
        self.states = dict(muted=True, steering={}, sensors=[dict(antid=a, boot_id=1, origin_ns=1000, samples=2048, output="antenna_l", amplitude=0.4, rf_gain={}) for a in range(8)])
        tx = CalibrationState(self.tx, "tx", 2.4e9, 80e6, 1, 1000)
        rx = CalibrationState(self.rx, "rx", 2.4e9, 10e6, 1, 1000, sample_origin_ns=1000)
        calibration = Calibration(tuple(CalibrationGroup((AntennaCorrection(s, 0, 0),)) for s in (tx, rx)))
        self.owner = SimpleNamespace(boards=[self.board], refresh_calibration=Mock(return_value=calibration))

    def run_measurement(self, capture):
        return measure(self.owner, [self.tx, self.rx], "reference", {(self.tx, self.rx): 0}, 1)

    def test_long_replay_and_receive_grid_transfer(self):
        period = 1024
        _, spectrum, _ = pilots(period, peak=128, mask=255)[0]
        # The full-rate grid starts 25 ns later. Its measured delay is 75 ns,
        # corresponding to 100 ns on the original calibrated RX grid.
        z = np.fft.ifft(spectrum * np.exp(-2j * np.pi * np.fft.fftfreq(period, 1 / 80e6) * 75e-9))
        packet = SimpleNamespace(decode_iq=lambda: z)
        capture = Mock()
        capture.collect.side_effect = lambda ids, samples, count, *args: {1: [[packet] for _ in range(count)]}
        with patch("espargos._iq_calibration.delay._rpc", return_value=self.states), patch("espargos._iq_calibration.delay._Captures", return_value=capture), patch("espargos._iq_calibration.delay._grid_origin", return_value=1025), patch(
            "espargos._iq_calibration.delay._waveform"
        ) as waveform:
            timing = self.run_measurement(capture)
        self.assertAlmostEqual(timing.delays_s[self.tx] * 1e9, 100, delta=0.1)
        self.assertEqual([c.args[1]["samples"] for c in waveform.call_args_list], [2048, 2048])
        self.assertEqual([c.args[1] for c in capture.collect.call_args_list], [1024, 1024])
        self.assertEqual(self.board.iq.set_config.call_args_list[0].args[0]["adc_decimation"], 1)
        self.assertEqual(self.board.iq.set_config.call_args.args[0]["adc_decimation"], 6)
        self.assertEqual(self.board.iq.set_config.call_args.args[0]["trigger"], self.config["trigger"])

    def test_probe_failure_restores_rate_and_leaves_tx_muted(self):
        capture = Mock()
        capture.collect.side_effect = TimeoutError("No IQ")
        with patch("espargos._iq_calibration.delay._rpc", return_value=self.states) as rpc, patch("espargos._iq_calibration.delay._Captures", return_value=capture), patch("espargos._iq_calibration.delay._waveform") as waveform:
            with self.assertRaises(TimeoutError):
                self.run_measurement(capture)
        self.assertEqual(self.board.iq.set_config.call_args.args[0]["adc_decimation"], 6)
        self.assertEqual(waveform.call_args.args[1]["type"], "restore")
        muting = [c.args[2]["muted"] for c in rpc.call_args_list if len(c.args) > 2 and "muted" in c.args[2]]
        self.assertTrue(muting[-1])
        capture.close.assert_called_once()

    def test_subgroup_rejected_before_changing_shared_adc_rate(self):
        self.config["modes"][2] = "iq_rx"
        with patch("espargos._iq_calibration.delay._rpc", return_value=self.states) as rpc:
            with self.assertRaisesRegex(ValueError, "all active receivers"):
                self.run_measurement(None)
        self.board.iq.set_config.assert_not_called()
        self.owner.refresh_calibration.assert_not_called()
        self.assertTrue(all(len(c.args) == 2 for c in rpc.call_args_list))
