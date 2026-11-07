import unittest
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import Mock, patch

import numpy as np

from espargos import Antenna
from espargos.steering import Beamspace, Weights
from espargos.waveforms import CW, LFM, Noise, IQSamples
from espargos.iq_transmitter import IQTransmitter
from espargos.tx_signal import TXSignals


def transmitter(boards=1):
    tx = IQTransmitter.__new__(IQTransmitter)
    tx.closed = False
    tx.boards = [SimpleNamespace(host=f"board{i}") for i in range(boards)]
    tx.states = [dict(modes=["iq_tx"] * 8, sensors=[dict(frequency_hz=2380e6, samples=1024, enabled=True, boot_id=1) for _ in range(8)], schedule_epoch=42) for _ in range(boards)]
    tx.positions = np.zeros((boards, 8, 2))
    tx.positions[:, :, 0] = np.arange(8) * 0.06
    tx._apply_signals = Mock()
    tx.signals = TXSignals(tx)
    return tx


class SignalTests(unittest.TestCase):
    def test_independent_sources_are_limited_per_sensor(self):
        tx = transmitter(4)
        with tx.update():
            for b in tx.boards:
                for a in range(8):
                    tx.signals.add(waveform=CW(offset_hz=(a + 1) * 1e6), antennas=[Antenna(b.host, a)])
        plans, _ = tx.signals._compile()
        self.assertEqual(len(tx.signals), 32)
        self.assertEqual([len(p) for p in plans], [8] * 4)
        self.assertEqual(plans[2][5][1][0][1]["waveform"]["frequency_hz"], 6e6)
        self.assertEqual(plans[2][5][0], [5])
        self.assertEqual(tx._apply_signals.call_count, 1)

    def test_sum_has_no_hidden_normalization(self):
        tx = transmitter()
        with tx.update():
            tx.signals.add(waveform=CW(), amplitude=0.1)
            tx.signals.add(waveform=Noise(), amplitude=0.2)
        records = tx.signals._compile()[0][0][0][1]
        self.assertEqual([r["waveform"]["amplitude"] for _, r in records], [1, 1])
        self.assertEqual([r["real"][0] for _, r in records], [0.1, 0.2])

    def test_validation_rolls_back_before_hardware(self):
        tx = transmitter()
        signal = tx.signals.add(waveform=CW())
        tx._apply_signals.reset_mock()
        with self.assertRaisesRegex(ValueError, "at most four"):
            with tx.update():
                signal.amplitude = 0.4
                for _ in range(4):
                    tx.signals.add(waveform=Noise())
        self.assertEqual(len(tx.signals), 1)
        self.assertEqual(signal.amplitude, 0.1)
        tx._apply_signals.assert_not_called()
        with self.assertRaises(ValueError):
            signal.amplitude = float("nan")
        self.assertEqual(signal.amplitude, 0.1)

    def test_removal_sends_silence_only_to_previous_targets(self):
        tx = transmitter()
        signal = tx.signals.add(waveform=CW(), antennas=[Antenna("board0", 3)])
        signal.remove()
        plans = tx._apply_signals.call_args.args[0]
        self.assertEqual(plans[0][0][0], [3])
        self.assertEqual(plans[0][0][1][0][1]["waveform"]["amplitude"], 0)

    def test_steering_changes_preserve_source(self):
        tx = transmitter()
        source = IQSamples(np.ones(1024, complex) * 0.5)
        signal = tx.signals.add(waveform=source, steering=Beamspace())
        signal.steering = Beamspace(azimuth_deg=30, squint=True)
        self.assertIs(signal.waveform, source)
        record = tx.signals._compile()[0][0][0][1][0][1]
        self.assertGreater(record["delay_samples"][1], 0)
        self.assertNotEqual(record["imag"][1], 0)

    def test_weights_and_waveform_units(self):
        tx = transmitter()
        a = Antenna("board0", 2)
        tx.signals.add(waveform=LFM(delay_s=1e-6), antennas=[a], steering=Weights({a: 0.25j}, {a: 25e-9}))
        record = tx.signals._compile()[0][0][0][1][0][1]
        self.assertEqual(record["imag"][2], 0.025)
        self.assertEqual(record["delay_samples"][2], 2)
        self.assertEqual(record["waveform"]["offset_samples"], 80)
        self.assertEqual(record["waveform"]["chirp_samples"], 32)

    def test_rejects_unconfigured_sensor_and_missing_geometry(self):
        tx = transmitter()
        tx.states[0]["modes"][2] = "wifi"
        with self.assertRaisesRegex(ValueError, "IQ TX"):
            tx.signals.add(waveform=CW(), antennas=[Antenna("board0", 2)])
        tx.positions = None
        with self.assertRaisesRegex(ValueError, "geometry"):
            tx.signals.add(waveform=CW(), steering=Beamspace())

    def test_scheduled_update_passes_boundary(self):
        tx = transmitter()
        s = tx.signals.add(waveform=CW())
        with tx.update(start_sample=8192):
            s.amplitude = 0.2
        self.assertEqual(tx._apply_signals.call_args.args[1], 8192)
        with self.assertRaises(ValueError):
            with tx.update(start_sample=7):
                pass

    def test_replay_preparation_only_when_required(self):
        tx = transmitter()
        del tx._apply_signals
        tx._parallel = lambda fn: [fn(i, b) for i, b in enumerate(tx.boards)]
        tx._edit_replay = Mock(return_value=nullcontext())
        tx.capture = Mock()
        tx.mute = Mock()
        with patch("espargos.iq_transmitter._waveform") as send:
            s = tx.signals.add(waveform=CW())
            s.steering = Beamspace(azimuth_deg=20)
        self.assertFalse(tx._edit_replay.call_args.args[-1])
        self.assertEqual(send.call_count, 2)

    def test_scheduled_groups_are_all_queued_before_waiting(self):
        tx = transmitter()
        with tx.update():
            tx.signals.add(waveform=CW(), antennas=[Antenna("board0", 0)])
            tx.signals.add(waveform=Noise(), antennas=[Antenna("board0", 1)])
        del tx._apply_signals
        tx.read = lambda: tx.states
        tx._parallel = lambda fn: [fn(0, tx.boards[0])]
        tx._edit_replay = lambda *args: nullcontext()
        tx.capture = Mock()
        tx.mute = Mock()
        events = []
        tx._wait_generation = lambda *args: events.append("wait")

        def send(*args):
            events.append("send")
            return {"generation": len(events)}

        with patch("espargos.iq_transmitter._rpc", side_effect=send):
            with tx.update(start_sample=8192):
                tx.signals[0].amplitude = 0.2
        self.assertEqual(events, ["send", "send", "wait", "wait"])

    def test_upload_cache_tracks_each_sensor_slot(self):
        tx = transmitter()
        tx.uploaded = {}
        tx.boards[0].control = Mock()
        tx._wait_generation = Mock()
        first = IQSamples(np.ones(1024, complex) * 0.25)._words
        second = IQSamples(np.ones(1024, complex) * 0.5j)._words
        tx._upload(0, 0, first, [0])
        tx._upload(0, 0, second, [1])
        calls = tx.boards[0].control.fetch.call_count
        tx._upload(0, 0, first, [0])
        tx._upload(0, 0, second, [1])
        self.assertEqual(tx.boards[0].control.fetch.call_count, calls)
        tx.states[0]["sensors"][0]["boot_id"] = 2
        tx._upload(0, 0, first, [0])
        self.assertGreater(tx.boards[0].control.fetch.call_count, calls)

    def test_mixing_limit_is_checked_before_sending(self):
        tx = transmitter()
        tx.signals.add(waveform=CW(), amplitude=0.8)
        tx._apply_signals.reset_mock()
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            tx.signals.add(waveform=CW(), amplitude=0.3)
        tx._apply_signals.assert_not_called()
        self.assertEqual(len(tx.signals), 1)

    def test_source_recipe_does_not_change_with_amplitude(self):
        tx = transmitter()
        s = tx.signals.add(waveform=Noise(), amplitude=0.1)
        before = tx.signals._compile()[0][0][0][1][0][1]
        s.amplitude = 0.3
        after = tx.signals._compile()[0][0][0][1][0][1]
        self.assertEqual(before["waveform"], after["waveform"])
        self.assertAlmostEqual(after["real"][0] / before["real"][0], 3)


if __name__ == "__main__":
    unittest.main()
