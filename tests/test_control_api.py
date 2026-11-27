import unittest
from unittest.mock import Mock

from espargos.control_api import ControllerResources
from espargos.control_legacy import LegacyControllerResources
from espargos.sensor import RFSwitchState


class ControlResourcesTest(unittest.TestCase):
    def setUp(self):
        self.control = Mock()
        self.control.post_json.return_value = "ok"
        self.api = ControllerResources(self.control)

    def test_scoped_iq_settings_use_one_request(self):
        self.api.write_radio("iq/rx", {
            "rf_switch": {2: 1, 6: 3},
            "receivers": [{"antid": 2, "rx_gain": 30}, {"antid": 4, "mode": "wifi_iq"}],
        })
        self.control.post_json.assert_called_once_with("api/v6/iq/rx", {"sensors": [
            {"antid": 2, "input": "reference", "rx_gain": 30},
            {"antid": 4, "mode": "wifi_iq"}, {"antid": 6, "input": "left"},
        ]})

    def test_wifi_gain_columns_round_trip_with_unordered_sensor_rows(self):
        values = [21, 30, 42, 13, 14, 25, 26, 27]
        self.control.get_json.return_value = {"sensors": [
            {"antid": i, "gain": {"rx_gain_value": values[i], "rx_gain_enable": True}} for i in reversed(range(8))
        ]}
        self.assertEqual(self.api.wifi_field("gain"), {"rx_gain_enable": [True] * 8, "rx_gain_value": values})
        self.api.set_wifi_field("gain", {"rx_gain_value": values, "rx_gain_enable": True})
        self.control.command.assert_called_once_with("api/v6/wifi/rx", {"gain": {"rx_gain_enable": True}, "sensors": [
            {"antid": i, "gain": {"rx_gain_value": v}} for i, v in enumerate(values)
        ]})

    def test_legacy_cannot_broadcast_a_sparse_write(self):
        legacy = LegacyControllerResources(self.control, api_major=5)
        with self.assertRaises(NotImplementedError):
            legacy.set_wifi_inputs({3: RFSwitchState(2)})
        self.assertEqual(self.control.mock_calls, [])
