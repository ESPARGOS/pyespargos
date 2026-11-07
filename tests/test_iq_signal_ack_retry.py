"""A lost final ACK must recover without waiting for another RF capture."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

from espargos.clustering import ClusterAssembler
from espargos.iq_receiver import IQReceiver


def receiver(send):
    rx = object.__new__(IQReceiver)
    rx.boards = [SimpleNamespace(iq=SimpleNamespace(acknowledge_signal_capture=send))]
    rx._logger = logging.getLogger(__name__)
    rx._signal_pending_ack = None
    rx._calibrating = False
    return rx


def test_final_failed_ack_is_retried_while_no_more_samples_arrive():
    send = Mock(side_effect=[TimeoutError(), TimeoutError(), TimeoutError(), None])
    rx = receiver(send)
    with patch("espargos.iq_receiver.time.sleep"), patch("espargos.iq_receiver.time.monotonic", return_value=10.0) as clock, patch.object(ClusterAssembler, "_expire_cluster_caches"):
        assert not rx._auto_acknowledge_signal_identity(7, 10, "acceptance")
        assert send.call_count == 3
        clock.return_value = 10.19
        rx._expire_cluster_caches()
        assert send.call_count == 3
        clock.return_value = 10.21
        rx._expire_cluster_caches()
        assert send.call_count == 4
        assert send.call_args.args == (7, 10)
        rx._expire_cluster_caches()
        assert send.call_count == 4


def test_later_successful_cumulative_ack_also_retires_failed_ack():
    send = Mock(side_effect=[TimeoutError(), TimeoutError(), TimeoutError(), None])
    rx = receiver(send)
    with patch("espargos.iq_receiver.time.sleep"), patch("espargos.iq_receiver.time.monotonic", return_value=10.0) as clock, patch.object(ClusterAssembler, "_expire_cluster_caches"):
        assert not rx._auto_acknowledge_signal_identity(7, 10, "acceptance")
        assert rx._auto_acknowledge_signal_identity(7, 11, "acceptance")
        clock.return_value = 11
        rx._expire_cluster_caches()
        assert send.call_count == 4
        assert send.call_args.args == (7, 11)


def test_retry_keeps_latest_failed_ack_and_waits_during_calibration():
    send = Mock(side_effect=[TimeoutError()] * 6 + [None])
    rx = receiver(send)
    with patch("espargos.iq_receiver.time.sleep"), patch("espargos.iq_receiver.time.monotonic", return_value=10.0) as clock, patch.object(ClusterAssembler, "_expire_cluster_caches"):
        assert not rx._auto_acknowledge_signal_identity(7, 10, "acceptance")
        assert not rx._auto_acknowledge_signal_identity(7, 11, "acceptance")
        clock.return_value = 11
        rx._calibrating = True
        rx._expire_cluster_caches()
        assert send.call_count == 6
        rx._calibrating = False
        rx._expire_cluster_caches()
        assert send.call_count == 7
        assert send.call_args.args == (7, 11)
