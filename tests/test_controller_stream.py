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

    def test_callback_can_stop_with_a_full_dispatch_queue(self):
        board = self.board()
        board._stream_lifecycle_lock = threading.RLock()
        board._stream_connected = True
        entered = threading.Event()
        release = threading.Event()
        drained = threading.Event()

        def consume(frame):
            if frame == b"first":
                entered.set()
                release.wait(2)
                board.stop()
            else:
                drained.set()

        board._stream_handle_message = consume
        with patch("espargos.board.STREAM_DISPATCH_QUEUE_PACKETS", 1):
            board._start_stream_dispatch()
        self.assertTrue(board._enqueue_stream_message(b"first"))
        self.assertTrue(entered.wait(2))
        self.assertTrue(board._enqueue_stream_message(b"last"))
        release.set()
        self.assertTrue(drained.wait(2))
        board.stop()
        self.assertIsNone(board._stream_dispatch_thread)

    def test_uplink_reuses_connected_transport_and_bounds_datagrams(self):
        b = self.board()
        b._stream_connected = True
        b._stream_transport = "udp"
        b._udp_sock = Mock()
        b._udp_remote_addr = ("192.0.2.1", 53330)
        b.send_stream_data(b"sample")
        b._udp_sock.sendto.assert_called_once_with(b"sample", b._udp_remote_addr)
        with self.assertRaises(ValueError):
            b.send_stream_data(bytes(1101))
        b._stream_transport = "websocket"
        b._stream_websocket = Mock()
        b.send_stream_data(b"indices")
        b._stream_websocket.send.assert_called_once_with(b"indices")
        b._stream_connected = False
        with self.assertRaises(ConnectionError):
            b.send_stream_data(b"sample")

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
