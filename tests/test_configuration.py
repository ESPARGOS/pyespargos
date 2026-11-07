"""Configuration entry points share validity policy and validate before writes."""

import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from espargos import Pool, ConfigurationUpdate
from espargos.ble_calibration import BLECalibration
from test_ble_api import FakeBoard


def seed(receiver, board_index=0):
    board = receiver.boards[board_index]
    receiver._calibrations[board_index] = BLECalibration(None, board_index, board.config["generation"], board.ble.packet_settings(board.config), 0, {a: (1, 2) for a in range(8)})


class ConfigurationTests(unittest.TestCase):
    def test_pool_gain_and_feed_preserve_receiver_calibration(self):
        board = FakeBoard()
        with Pool([board]) as pool:
            receiver = pool.ble
            seed(receiver)
            for settings in ({"rx_gain": 25}, {"gain_mode": "auto"}, {"output": "antenna_l"}, {"output": "random"}):
                pool.configure(**settings)
                self.assertTrue(receiver.is_calibrated(), settings)
                self.assertEqual(receiver._calibrations[0].generation, board.config["generation"])
            pool.configure(channel=38)
            self.assertFalse(receiver.is_calibrated())

    def test_validate_every_board_before_writing(self):
        a, b = FakeBoard("a"), FakeBoard("b")
        b.modes[7] = "wifi"
        with Pool([a, b]) as pool:
            with self.assertRaises(ValueError):
                pool.configure(rx_gain=30)
            self.assertFalse(a.writes)
            self.assertFalse(b.writes)

    def test_failed_ble_write_discards_calibration(self):
        board = FakeBoard()
        with Pool([board]) as pool:
            receiver = pool.ble
            seed(receiver)
            with patch.object(board.ble, "set_config", side_effect=OSError("disconnected")):
                with self.assertRaises(OSError):
                    pool.configure(rx_gain=30)
            self.assertFalse(receiver.is_calibrated())

    def test_extension_plans_and_applies_without_pool_mode_branches(self):
        seen = []

        def plan(ctx, options):
            if set(options) != {"frequency_hz"}:
                raise ValueError("frequency required")
            seen.append(("plan", ctx.board.host))
            return ConfigurationUpdate(after_modes=lambda: seen.append(("apply", ctx.board.host, options["frequency_hz"])))

        Pool.register_configuration("test_preamble_iq", plan)
        boards = [FakeBoard("a"), FakeBoard("b")]
        for board in boards:
            board.iq.set_config = lambda cfg, board=board: setattr(board, "modes", cfg["modes"])
        with Pool(boards) as pool, patch("espargos.configuration._wait"):
            rx = pool.ble
            seed(rx, 0)
            pool.configure(mode="test_preamble_iq", frequency_hz=2412e6)
            self.assertFalse(rx.is_calibrated())
            self.assertEqual(seen, [("plan", "a"), ("plan", "b"), ("apply", "a", 2412e6), ("apply", "b", 2412e6)])
            self.assertFalse(rx._expected.any())

    def test_iq_mode_switch_carries_frequency_and_disables_tx(self):
        board = FakeBoard()
        board.modes = ["iq_rx"] * 8
        board.iq.get_config = lambda: {"modes": board.modes, "receivers": [{"rf_freq_hz": 2440e6}] * 8, "adc_decimation": 4}
        board.iq.set_config = Mock(side_effect=lambda cfg: setattr(board, "modes", cfg.get("modes", board.modes)))
        with Pool([board]) as pool, patch("espargos.configuration._wait"):
            pool.configure(mode="iq_tx")
            tx = next(value["transmitters"] for path, value in board.writes if path == "set_iq_tx" and "transmitters" in value)
            self.assertTrue(all(x["amplitude"] == 0 and x["frequency_hz"] == 2440e6 for x in tx))
            original = board.control.get_json
            board.control.get_json = lambda path: {"sensors": [{"frequency_hz": 2450e6}] * 8} if path == "get_iq_tx" else original(path)
            pool.configure(mode="iq_rx")
            updates = board.iq.set_config.call_args.args[0]["receivers"]
            self.assertTrue(all(x["rf_freq_hz"] == 2450e6 for x in updates))

    def test_wifi_channel_plan_preserves_unselected_antennas(self):
        board = FakeBoard()
        board.modes = ["wifi"] * 8
        board.wifi_rx = SimpleNamespace(get_channel_overrides=lambda: {"override_active": True, "channel-primary": [1] * 8, "channel-secondary": [0] * 8}, set_channel_overrides=Mock())
        with Pool([board]) as pool:
            pool.select(antennas=[2, 5]).configure(channel=6, secondary_channel=-1)
            actual = board.wifi_rx.set_channel_overrides.call_args.args[0]
            self.assertEqual(actual["channel-primary"], [1, 1, 6, 1, 1, 6, 1, 1])
            self.assertEqual(actual["channel-secondary"], [0, 0, 2, 0, 0, 2, 0, 0])

    def test_wifi_receiver_owns_channel_failure_invalidation(self):
        from espargos.wifi_transceiver import WiFiTransceiver

        board = SimpleNamespace(host="wifi", wifi_rx=SimpleNamespace(set_channel_overrides=Mock(side_effect=OSError("lost reply"))))
        receiver = SimpleNamespace(boards=[board], invalidate_calibration=Mock())
        with self.assertRaises(OSError):
            WiFiTransceiver.set_wifi_channel_overrides(receiver, {"channel-primary": [6] * 8}, board_index=0, antennas=[2, 5])
        invalidated = receiver.invalidate_calibration.call_args.kwargs["antennas"]
        self.assertEqual([a.antid for a in invalidated], [2, 5])
