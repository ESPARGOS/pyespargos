import logging
import socket
from unittest.mock import Mock, patch

from espargos.board import Board, EspargosHTTPStatusError, STREAM_MAGIC


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
    sock.recvfrom.side_effect = [(b"data", remote), (STREAM_MAGIC, ("192.168.1.3", 5000)), (STREAM_MAGIC, ("fe80::2", 1000, 0, 3)), (STREAM_MAGIC, remote)]
    with patch("espargos.board.socket.socket", return_value=sock), patch("espargos.board.threading.Thread"):
        assert b._try_start_udp() is None
    sock.bind.assert_called_once_with(("::", 0))
    sock.sendto.assert_called_once_with(b"", remote)
    assert sock.recvfrom.call_count == 4
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
    b._disable_udp_stream.assert_called_once_with(12345)


def test_udp_only_tries_legacy_registration_for_missing_endpoint():
    for failure, paths in [(TimeoutError(), ["stream_udp"]), (EspargosHTTPStatusError(503), ["stream_udp"]), (EspargosHTTPStatusError(404), ["stream_udp", "csi_udp"])]:
        b = board()
        b.control = Mock()
        b.control.command.side_effect = [failure, None, None]
        error = Board._enable_udp_stream(b, 12345)
        assert (error is None) == (len(paths) == 2)
        assert [args.args[0] for args in b.control.command.call_args_list] == paths
        if error is None:
            Board._disable_udp_stream(b, 12345)
            b.control.command.assert_called_with("csi_udp", {"enable": False, "port": 12345})
