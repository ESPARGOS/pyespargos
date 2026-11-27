"""Recover a missing TX status response without repeating hardware changes."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from espargos.control_legacy import LegacyControllerResources
from espargos.board import EspargosHTTPStatusError
from espargos.iq_tx_control import _rpc


class TXControlTests(TestCase):
    def test_status_allocation_failure_reads_back_applied_settings(self):
        for message in ("not enough memory for JSON response", "not enough memory for TX status"):
            with self.subTest(message=message):
                control = Mock()
                control.post_json.side_effect = EspargosHTTPStatusError(503, "set_iq_tx", message)
                control.get_json.return_value = {"muted": True}
                self.assertEqual(_rpc(SimpleNamespace(_resources=LegacyControllerResources(control)), "iq/tx", {"muted": True}), {"muted": True})
                control.post_json.assert_called_once_with("set_iq_tx", {"muted": True})
                control.get_json.assert_called_once_with("get_iq_tx")

    def test_other_failures_are_not_treated_as_successful_writes(self):
        for path, status, message in [("iq/tx", 503, "busy"), ("iq/tx", 500, "sensor mode or TX control failed"), ("iq/tx/synthesize", 503, "not enough memory for JSON response")]:
            with self.subTest(path=path, status=status):
                control = Mock()
                control.post_json.side_effect = EspargosHTTPStatusError(status, path, message)
                with self.assertRaises(EspargosHTTPStatusError):
                    _rpc(SimpleNamespace(_resources=LegacyControllerResources(control)), path, {})
                control.get_json.assert_not_called()
                control.post_json.assert_called_once()
