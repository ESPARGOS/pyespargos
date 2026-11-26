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
        capture.collect.side_effect = lambda ids, samples, count, *args, **kwargs: {1: [[packet] for _ in range(count)]}
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

    def test_group_probes_only_three_transmitters_and_covers_all_five(self):
        from dataclasses import replace

        self.config["modes"] = ["iq_tx"] * 5 + ["iq_rx"] * 3
        tx, rx = [Antenna("array", a) for a in range(5)], [Antenna("array", a) for a in range(5, 8)]
        groups = []
        for group, antennas in zip(self.owner.refresh_calibration.return_value.groups, (tx, rx)):
            template = group.corrections[0]
            groups.append(CalibrationGroup(tuple(replace(template, state=replace(template.state, antenna=a)) for a in antennas)))
        self.owner.refresh_calibration.return_value = Calibration(tuple(groups))
        _, spectrum, _ = pilots(1024, peak=128, mask=255)[0]
        frequencies = np.fft.fftfreq(1024, 1 / 80e6)
        # One link clips, one is missing and one measures 120 ns including flight time.
        packet = SimpleNamespace(decode_iq=lambda: np.fft.ifft(spectrum * np.exp(-2j * np.pi * frequencies * 120e-9)))
        clipped = SimpleNamespace(decode_iq=lambda: np.full(1024, 511))
        capture = Mock()
        capture.collect.side_effect = lambda ids, size, count, *args, **kw: {5: [[packet]] * count, 6: [[clipped]] * count}
        paths = {(t, r): 20e-9 for t in tx[:4] for r in rx}
        with patch("espargos._iq_calibration.delay._rpc", return_value=self.states), patch("espargos._iq_calibration.delay._Captures", return_value=capture), patch("espargos._iq_calibration.delay._grid_origin", return_value=1000), patch(
            "espargos._iq_calibration.delay._waveform"
        ) as waveform:
            result = measure(self.owner, tx + rx, "antenna_r", paths, 1, method="group")
        self.assertEqual(set(result.delays_s), set(tx))
        np.testing.assert_allclose(list(result.delays_s.values()), 100e-9, atol=0.1e-9)
        turns = [c.args[1]["antennas"][0] for c in waveform.call_args_list if c.args[1]["type"] == "calibration"]
        self.assertEqual(len(turns), 3)
        self.assertEqual(len(set(turns)), 3)
        self.assertNotIn(tx[-1].antid, turns)

        # Four candidate transmitters, three votes required. After two empty
        # turns even perfect observations from the last two cannot establish agreement.
        capture.collect.side_effect = lambda *args, **kwargs: {}
        with patch("espargos._iq_calibration.delay._rpc", return_value=self.states) as rpc, patch("espargos._iq_calibration.delay._Captures", return_value=capture), patch("espargos._iq_calibration.delay._waveform") as waveform:
            with self.assertRaisesRegex(ValueError, "0 usable, 2 probed"):
                measure(self.owner, tx + rx, "antenna_r", paths, 1, method="group")
        self.assertEqual([call.args[1]["type"] for call in waveform.call_args_list], ["calibration", "calibration", "restore", "restore"])
        self.assertTrue([call.args[2]["muted"] for call in rpc.call_args_list if len(call.args) > 2 and "muted" in call.args[2]][-1])
