import http.client
import threading
import unittest
from unittest.mock import Mock, patch
from espargos.board import Board, EspargosHTTPStatusError


class HTTPTests(unittest.TestCase):
    def board(self):
        b = object.__new__(Board)
        b.host = "array"
        b._uart_client = None
        b._http_lock = threading.Lock()
        b._http_connection = None
        return b

    def connection(self):
        c = Mock()
        c.getresponse.return_value.status = 200
        c.getresponse.return_value.read.return_value = b"ok"
        return c

    def test_reuses_connection(self):
        b = self.board()
        with patch("espargos.board.http.client.HTTPConnection", return_value=self.connection()) as factory:
            for i in range(10):
                self.assertEqual(b._fetch("status"), "ok")
            factory.assert_called_once()

    def test_reconnects_closed_read_connection(self):
        b = self.board()
        c = self.connection()
        c.request.side_effect = http.client.RemoteDisconnected()
        with patch("espargos.board.http.client.HTTPConnection", side_effect=[c, self.connection()]) as factory:
            self.assertEqual(b._fetch("status"), "ok")
            self.assertEqual(factory.call_count, 2)
            c.close.assert_called_once()

    def test_does_not_repeat_ambiguous_write(self):
        b = self.board()
        c = self.connection()
        c.getresponse.side_effect = http.client.RemoteDisconnected()
        with patch("espargos.board.http.client.HTTPConnection", return_value=c) as factory:
            with self.assertRaises(http.client.RemoteDisconnected):
                b._fetch("settings", "{}")
            factory.assert_called_once()
            self.assertIsNone(b._http_connection)

    def test_does_not_retry_server_error(self):
        b = self.board()
        c = self.connection()
        c.getresponse.return_value.status = 500
        with patch("espargos.board.http.client.HTTPConnection", return_value=c) as factory:
            with self.assertRaises(EspargosHTTPStatusError):
                b._fetch("status")
            factory.assert_called_once()

    def test_retries_temporary_read_exhaustion_once(self):
        b = self.board()
        c = self.connection()
        busy = Mock(status=503)
        busy.read.return_value = b"not enough memory"
        ok = Mock(status=200)
        ok.read.return_value = b"ok"
        c.getresponse.side_effect = [busy, ok]
        with patch("espargos.board.http.client.HTTPConnection", return_value=c), patch("espargos.board.time.sleep") as sleep:
            self.assertEqual(b._fetch("status"), "ok")
            self.assertEqual(c.request.call_count, 2)
            sleep.assert_called_once_with(0.1)

    def test_temporary_read_retry_is_bounded(self):
        b = self.board()
        c = self.connection()
        c.getresponse.return_value.status = 503
        with patch("espargos.board.http.client.HTTPConnection", return_value=c), patch("espargos.board.time.sleep"):
            with self.assertRaises(EspargosHTTPStatusError):
                b._fetch("status")
            self.assertEqual(c.request.call_count, 2)

    def test_does_not_retry_unavailable_write(self):
        b = self.board()
        c = self.connection()
        c.getresponse.return_value.status = 503
        with patch("espargos.board.http.client.HTTPConnection", return_value=c):
            with self.assertRaises(EspargosHTTPStatusError):
                b._fetch("settings", "{}")
            c.request.assert_called_once()

    def test_action_gets_are_not_retried(self):
        for path in ["reboot", "factoryreset"]:
            b = self.board()
            c = self.connection()
            c.getresponse.side_effect = ConnectionResetError()
            with patch("espargos.board.http.client.HTTPConnection", return_value=c):
                with self.assertRaises(ConnectionResetError):
                    b._fetch(path)
                c.request.assert_called_once()

    def test_interrupted_response_does_not_poison_cleanup_requests(self):
        b = self.board()
        interrupted = self.connection()
        interrupted.getresponse.side_effect = KeyboardInterrupt()
        clean = self.connection()
        with patch("espargos.board.http.client.HTTPConnection", side_effect=[interrupted, clean]) as factory:
            with self.assertRaises(KeyboardInterrupt):
                b._fetch("status")
            interrupted.close.assert_called_once()
            self.assertIsNone(b._http_connection)
            self.assertEqual(b._fetch("set_iq_tx", '{"muted":true}'), "ok")
            self.assertEqual(factory.call_count, 2)
