"""A lost final ACK must recover without waiting for another RF capture."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

from espargos.clustering import ClusterAssembler
from espargos.iq_receiver import IQReceiver, _BoardSignalTransport


def receiver(send):
    rx = object.__new__(IQReceiver)
    rx.boards = [SimpleNamespace(iq=SimpleNamespace(acknowledge_signal_capture=send))]
    rx._logger = logging.getLogger(__name__)
    rx._signal_transports = [_BoardSignalTransport(rx, rx.boards[0])]
    rx._calibrating = False
    return rx


def test_final_failed_ack_is_retried_while_no_more_samples_arrive():
    send = Mock(side_effect=[TimeoutError(), TimeoutError(), TimeoutError(), None])
    rx = receiver(send)
    with patch("espargos.iq_receiver.time.sleep"), patch("espargos.iq_receiver.time.monotonic", return_value=10.0) as clock, patch.object(ClusterAssembler, "_expire_cluster_caches"):
        assert not rx._signal_transports[0].acknowledge(7, 10, "acceptance")
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
        assert not rx._signal_transports[0].acknowledge(7, 10, "acceptance")
        assert rx._signal_transports[0].acknowledge(7, 11, "acceptance")
        clock.return_value = 11
        rx._expire_cluster_caches()
        assert send.call_count == 4
        assert send.call_args.args == (7, 11)


def test_retry_keeps_latest_failed_ack_and_waits_during_calibration():
    send = Mock(side_effect=[TimeoutError()] * 6 + [None])
    rx = receiver(send)
    with patch("espargos.iq_receiver.time.sleep"), patch("espargos.iq_receiver.time.monotonic", return_value=10.0) as clock, patch.object(ClusterAssembler, "_expire_cluster_caches"):
        assert not rx._signal_transports[0].acknowledge(7, 10, "acceptance")
        assert not rx._signal_transports[0].acknowledge(7, 11, "acceptance")
        clock.return_value = 11
        rx._calibrating = True
        rx._expire_cluster_caches()
        assert send.call_count == 6
        rx._calibrating = False
        rx._expire_cluster_caches()
        assert send.call_count == 7
        assert send.call_args.args == (7, 11)


def test_two_boards_retire_independent_events_and_retry_only_the_failed_board():
    from test_iq_signal_capture import _Revision, _payload, IQ_SIGNAL_BANK_CHUNKS
    from unittest.mock import call

    boards = [SimpleNamespace(iq=Mock(), revision=_Revision()) for _ in range(2)]
    rx = IQReceiver(SimpleNamespace(boards=boards, processing=False))
    first, second = (board.iq.acknowledge_signal_capture for board in boards)
    first.side_effect = [None] * 5 + [TimeoutError()] * 3 + [None]
    try:
        with patch("espargos.iq_receiver.time.monotonic", return_value=10.0) as clock:
            for capture_id in range(7):
                for offset in range(IQ_SIGNAL_BANK_CHUNKS):
                    for board_index in range(2):
                        # Only the healthy board produces the seventh event.
                        if capture_id == 6 and board_index == 0:
                            continue
                        for antenna in range(8):
                            payload = _payload(antenna, capture_id, offset, chunk_count=IQ_SIGNAL_BANK_CHUNKS)
                            payload.is_signal_capture = True
                            message = SimpleNamespace(antenna_id=antenna, payload=payload)
                            rx._input_queue.put_nowait((board_index, message))
                        rx._run(timeout=0)
            expected = [call(7, i) for i in range(6)]
            assert first.call_args_list == expected + [call(7, 5)] * 2
            assert second.call_args_list == expected + [call(7, 6)]
            clock.return_value += 1
            rx._expire_cluster_caches()
            assert first.call_args_list == expected + [call(7, 5)] * 3
            assert second.call_args_list == expected + [call(7, 6)]
    finally:
        rx._close()


def test_late_older_event_does_not_clear_a_newer_pending_ack():
    send = Mock(side_effect=[TimeoutError(), TimeoutError(), TimeoutError(), None])
    transport = receiver(send)._signal_transports[0]
    with patch("espargos.iq_receiver.time.sleep"):
        assert not transport.acknowledge(7, 0, "acceptance")
        assert transport.acknowledge(7, 0xFFFFFFFE, "late rejection")
    assert send.call_args.args == (7, 0)
