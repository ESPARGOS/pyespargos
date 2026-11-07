import logging
import threading
import unittest
from unittest.mock import Mock, patch

from espargos.board import Board, EspargosHTTPStatusError


class ControllerStreamTests(unittest.TestCase):
    def board(self):
        b = Board.__new__(Board)
        b._sensor_message_subscriptions_lock = threading.Lock()
        b._controller_message_subscriptions = {}
        b._sensor_message_subscriptions = {}
        b._logger = logging.getLogger("test")
        b._uart_client = None
        b._http_lock = threading.Lock()
        b._http_connection = None
        b.host = "localhost"
        return b

    def test_controller_frames_share_dispatch_without_sensor_reassembly(self):
        b = self.board()
        b._sensor_message_reassembler = Mock()
        received = []
        s = b.subscribe_controller_messages(0x314D5149, received.append)
        frame = b"IQM1" + bytes(60)
        b._stream_handle_message(frame)
        self.assertEqual(received, [frame])
        b._sensor_message_reassembler.push.assert_not_called()
        self.assertTrue(b.unsubscribe_controller_messages(s))
        self.assertFalse(b.unsubscribe_controller_messages(s))
        self.assertEqual(b._controller_message_subscriptions, {})

    def test_http_accepted_is_not_completion_or_an_error(self):
        b = self.board()
        with patch("espargos.board.http.client.HTTPConnection") as factory:
            conn = factory.return_value
            response = conn.getresponse.return_value
            response.status = 202
            response.read.return_value = b"sent; verify generation"
            self.assertEqual(b._fetch("upload_iq_tx_waveform", b"data"), "sent; verify generation")
            conn.close.assert_not_called()
            response.status = 409
            with self.assertRaises(EspargosHTTPStatusError):
                b._fetch("upload_iq_tx_waveform", b"data")
            self.assertEqual(conn.close.call_count, 1)


if __name__ == "__main__":
    unittest.main()
