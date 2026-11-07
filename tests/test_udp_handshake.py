import logging
import socket
from unittest.mock import Mock, patch

from espargos.board import Board, STREAM_MAGIC


def board():
    b = Board.__new__(Board)
    b.host = "192.168.1.2"
    b.netconf = {"hostname": "test"}
    b._logger = logging.getLogger("test")
    b._enable_udp_stream = Mock(return_value=None)
    b._disable_udp_stream = Mock()
    return b


def test_udp_handshake_allows_data_before_magic():
    b = board()
    remote = ("192.168.1.2", 5000)
    sock = Mock()
    sock.getsockname.return_value = ("0.0.0.0", 12345)
    sock.recvfrom.side_effect = [(b"data", remote), (STREAM_MAGIC, ("192.168.1.3", 5000)), (STREAM_MAGIC, remote)]
    with patch("espargos.board.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_DGRAM, 0, "", remote)]), patch("espargos.board.socket.socket", return_value=sock), patch("espargos.board.threading.Thread"):
        assert b._try_start_udp() is None
    assert sock.recvfrom.call_count == 3
    assert b._stream_transport == "udp"
    b._disable_udp_stream.assert_not_called()


def test_udp_handshake_timeout_still_cleans_up():
    b = board()
    remote = ("192.168.1.2", 5000)
    sock = Mock()
    sock.getsockname.return_value = ("0.0.0.0", 12345)
    sock.recvfrom.side_effect = socket.timeout()
    with patch("espargos.board.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_DGRAM, 0, "", remote)]), patch("espargos.board.socket.socket", return_value=sock):
        assert "Timeout" in b._try_start_udp()
    sock.close.assert_called_once()
    b._disable_udp_stream.assert_called_once()
