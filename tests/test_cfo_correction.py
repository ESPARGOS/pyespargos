import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from espargos.cfo import correction_hz_to_raw, correction_raw_to_hz
from espargos.board_wifi_rx import WiFiRxCapability
from espargos.wifi_transceiver import WiFiTransceiver


class CFOCorrectionTest(unittest.TestCase):
    def test_all_register_values_round_trip(self):
        for raw in range(-4096, 4096):
            self.assertEqual(correction_hz_to_raw(correction_raw_to_hz(raw)), raw)

    def test_quantization_and_validation(self):
        self.assertEqual(correction_hz_to_raw(10000), -131)
        self.assertEqual(correction_hz_to_raw(-10000), 131)
        self.assertEqual(correction_hz_to_raw(0), 0)
        self.assertEqual(correction_raw_to_hz(-131), 9994.5068359375)
        for hz in [float("nan"), float("inf"), -float("inf"), -312424, 312501]:
            with self.assertRaises(ValueError):
                correction_hz_to_raw(hz)

    def test_measured_channel10_packet(self):
        # HT transmitter 0a:ee:f5:00:00:01, cfo_high_rate=506: negative
        # packet CFO needs a positive NRXFOE value to maintain reception.
        self.assertEqual(correction_hz_to_raw(-19302.3681640625), 253)
        self.assertEqual(correction_raw_to_hz(253), -19302.3681640625)

    def test_board_uses_existing_wire_api(self):
        rx = WiFiRxCapability(SimpleNamespace(control=Mock()))
        rx.set_cfo_correction_hz(False, 10000)
        rx._board.control.command.assert_called_once_with("set_cfo_correction", {"auto": False, "value": -131})
        rx._board.control.get_json.return_value = {"auto": False, "value": -131}
        self.assertEqual(rx.get_cfo_correction_hz(), {"auto": False, "value_hz": 9994.5068359375})
        rx.set_cfo_correction_hz(**rx.get_cfo_correction_hz())
        rx._board.control.command.reset_mock()
        with self.assertRaises(ValueError):
            rx.set_cfo_correction_hz(False, float("nan"))
        rx._board.control.command.assert_not_called()

    def test_pool_uses_existing_reconciliation(self):
        pool = SimpleNamespace(set_cfo_correction=Mock(), get_cfo_correction=Mock(return_value={"auto": True, "value": 131}))
        WiFiTransceiver.set_cfo_correction_hz(pool, False, -10000)
        pool.set_cfo_correction.assert_called_once_with(False, 131)
        self.assertEqual(WiFiTransceiver.get_cfo_correction_hz(pool), {"auto": True, "value_hz": -9994.5068359375})


if __name__ == "__main__":
    unittest.main()
