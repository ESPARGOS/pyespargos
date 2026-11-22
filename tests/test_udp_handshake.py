import logging
import socket
from unittest.mock import Mock, patch

from espargos.board import Board, STREAM_MAGIC


def board():
    b = Board.__new__(Board)
    b.host = "192.168.1.2"
    b._http_peer = (socket.AF_INET, ("192.168.1.2", 80))
    b.netconf = {"hostname": "test"}
    b._logger = logging.getLogger("test")
    b._enable_udp_stream = Mock(return_value=None)
    b._disable_udp_stream = Mock()
    return b


def test_udp_handshake_allows_data_before_magic():
    b = board()
    b.host = "test.local"
    b._http_peer = (socket.AF_INET6, ("fe80::2", 80, 0, 3))
    remote = ("fe80::2", 53330, 0, 3)
    sock = Mock()
    sock.getsockname.return_value = ("::", 12345, 0, 0)
    sock.recvfrom.side_effect = [(b"data", remote), (STREAM_MAGIC, ("192.168.1.3", 5000)), (STREAM_MAGIC, remote)]
    with patch("espargos.board.socket.socket", return_value=sock), patch("espargos.board.threading.Thread"):
        assert b._try_start_udp() is None
    sock.bind.assert_called_once_with(("::", 0))
    sock.sendto.assert_called_once_with(b"", remote)
    assert sock.recvfrom.call_count == 3
    assert b._stream_transport == "udp"
    b._disable_udp_stream.assert_not_called()


def test_udp_handshake_timeout_still_cleans_up():
    b = board()
    sock = Mock()
    sock.getsockname.return_value = ("0.0.0.0", 12345)
    sock.recvfrom.side_effect = socket.timeout()
    with patch("espargos.board.socket.socket", return_value=sock):
        assert "Timeout" in b._try_start_udp()
    sock.close.assert_called_once()
    b._disable_udp_stream.assert_called_once()
