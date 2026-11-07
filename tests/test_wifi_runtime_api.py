"""Runtime configuration must not fall back to persistent writes on real errors."""
import unittest
import json
from unittest.mock import Mock, patch
from espargos.board import Board, BoardControl, EspargosHTTPStatusError, EspargosAPIVersionError
from espargos.board_wifi_rx import WiFiRxCapability


class WiFiRuntimeAPITests(unittest.TestCase):
    def test_runtime_read_and_write_never_access_defaults(self):
        board = Mock()
        wifi = WiFiRxCapability(board)
        board.control.get_json.return_value = {'channel-primary': 8}
        self.assertEqual(wifi.get_config(), {'channel-primary': 8})
        wifi.set_config({'channel-primary': 9})
        board.control.get_json.assert_called_once_with('get_wifi_runtime')
        board.control.command.assert_called_once_with('set_wifi_runtime', {'channel-primary': 9})

    def test_legacy_fallback_only_on_older_firmware_and_missing_endpoint(self):
        for major in (3, 4, 5):
            for status in (404, 400, 409, 500, 503):
                for reading in (True, False):
                    with self.subTest(major=major, status=status, reading=reading):
                        board = Mock(api_version=(major, 0))
                        wifi = WiFiRxCapability(board)
                        method = board.control.get_json if reading else board.control.command
                        method.side_effect = [EspargosHTTPStatusError(status), {'channel-primary': 5}]
                        invoke = wifi.get_config if reading else lambda: wifi.set_config({'channel-primary': 6})
                        if status == 404 and major < 5:
                            invoke()
                            self.assertEqual([c.args[0] for c in method.call_args_list],
                                             ['get_wifi_runtime', 'get_wificonf'] if reading else ['set_wifi_runtime', 'set_wificonf'])
                        else:
                            with self.assertRaises(EspargosHTTPStatusError):
                                invoke()
                            self.assertEqual(method.call_count, 1)

    def test_supported_firmware_versions_and_iq_access(self):
        for major in (2, 3, 4, 5, 6):
            with self.subTest(major=major):
                replies = {
                    'api_info': {'device': 'espargos', 'revision': 'densiflorus', 'api-major': major, 'api-minor': 0},
                    'get_netconf': {'hostname': 'test'}, 'get_ip_info': {'ip': '127.0.0.1'},
                }
                with patch.object(BoardControl, 'fetch', side_effect=lambda path: json.dumps(replies[path])):
                    if major in (2, 6):
                        with self.assertRaises(EspargosAPIVersionError):
                            Board('127.0.0.1')
                        continue
                    board = Board('127.0.0.1')
                    self.assertEqual(board.api_version, (major, 0))
                    if major == 3:
                        with self.assertRaises(EspargosAPIVersionError):
                            _ = board.iq
                    else:
                        self.assertIsNotNone(board.iq)

    def test_transport_failure_does_not_retry_as_persistent_write(self):
        board = Mock()
        board.control.command.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            WiFiRxCapability(board).set_config({'channel-primary': 6})
        board.control.command.assert_called_once_with('set_wifi_runtime', {'channel-primary': 6})
