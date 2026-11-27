import logging
import threading
import unittest
from types import SimpleNamespace

from espargos.control_legacy import LegacyControllerResources
from espargos.iq_receiver import _BoardSignalTransport


def test_reference_readiness_and_capture_generation():
    from unittest.mock import Mock, patch

    import numpy as np
    import pytest
    from espargos._iq_calibration import reference

    gen = 12
    source = np.random.default_rng(94).normal(size=1024)
    ids = [1, 5]
    for missing in ("ready", "chunk", None):
        board = Mock(host="test-board")
        board._resources = LegacyControllerResources(board.control)
        pool = SimpleNamespace(boards=[board], master_board=board, _reference_sequence=7, clear_chunks=Mock(), _logger=Mock())
        scope = SimpleNamespace(active=[0], pairs=[(0, a) for a in ids], ids=[ids], apply_rx=Mock())
        board.iq.get_config.return_value = {"config_generation": gen}

        def emit(antenna, flag, generation, offset=0):
            handler = board.subscribe_sensor_messages.call_args.args[1]
            packet = SimpleNamespace(flags=flag, config_generation=generation, capture_chunk_offset=offset, fire_time_ns=1000 * antenna, producer_wake_write_ptr=1, decode_iq=lambda: source[256 * offset : 256 * (offset + 1)])
            handler(SimpleNamespace(antenna_id=antenna, payload=packet))

        def arm(config):
            if config["trigger"]["mode"] == "reference":
                for antenna in ids:
                    stale = missing == "ready" and antenna == 5
                    emit(antenna, reference.READY, gen - int(stale))

        def gate(path, payload):
            if payload["open"] and scope.apply_rx.called:
                for antenna in ids:
                    for offset in range(4):
                        stale = missing == "chunk" and antenna == 5 and offset == 1
                        emit(antenna, reference.REFERENCE, gen - int(stale), offset)

        scope.apply_rx.side_effect = arm
        board.control.command.side_effect = gate
        with patch.object(reference, "IQChunkPacket", side_effect=lambda packet: packet):
            if missing:
                detail = "readiness" if missing == "ready" else "onset snapshots"
                with pytest.raises(RuntimeError, match=detail + ": test-board/5"):
                    reference._acquire_grid(pool, 2437e6, timeout=0.001, scope=scope)
                board.iq.set_sync_anchor.assert_not_called()
            else:
                assert reference._acquire_grid(pool, 2437e6, timeout=0.01, scope=scope) == [8]
                board.iq.set_sync_anchor.assert_called_once_with([0, 1000, 0, 0, 0, 5000, 0, 0], seq=8)
        board.unsubscribe_sensor_messages.assert_called_once()
        assert board.control.command.call_args.args == ("gate_iq_reference", {"open": True})


class IQPoolReadyTests(unittest.TestCase):
    @staticmethod
    def _recovery_pool():
        receiver = SimpleNamespace(_logger=logging.getLogger(__name__))
        return _BoardSignalTransport(receiver, SimpleNamespace(iq=SimpleNamespace()))

    def test_consecutive_signal_epoch_mismatches_reset_barrier_without_resync(self):
        pool = self._recovery_pool()
        called = threading.Event()
        calls = []

        def sync():
            calls.append(True)
            called.set()

        pool.sync = sync
        rearmed = []
        pool.board.iq.rearm_signal_capture = lambda: rearmed.append(True)
        reason = "array source epoch mismatch in Signal event 12"
        pool._signal_capture_rejected(reason)
        pool._signal_capture_rejected(reason)
        self.assertFalse(called.is_set())
        pool._signal_capture_rejected(reason)
        # The worker may still be completing its transport reset.
        for _ in range(100):
            if rearmed:
                break
            threading.Event().wait(0.001)
        self.assertFalse(called.is_set())
        self.assertEqual(len(calls), 0)
        self.assertEqual(rearmed, [True])

    def test_valid_signal_capture_breaks_epoch_mismatch_streak(self):
        pool = self._recovery_pool()
        called = threading.Event()
        pool.sync = called.set
        reason = "array source epoch mismatch in Signal event 20"
        pool._signal_capture_rejected(reason)
        pool._signal_capture_rejected(reason)
        pool._signal_capture_accepted()
        pool._signal_capture_rejected(reason)
        pool._signal_capture_rejected(reason)
        self.assertFalse(called.wait(0.02))

    def test_incomplete_signal_event_resets_barrier_without_resync(self):
        pool = self._recovery_pool()
        synced = threading.Event()
        reset = threading.Event()
        pool.sync = synced.set
        pool.board.iq.rearm_signal_capture = reset.set
        pool._signal_capture_rejected("settled incomplete sensor set in Signal event 7, offset 0")
        self.assertTrue(reset.wait(1.0))
        self.assertFalse(synced.is_set())

    def test_superseded_incomplete_signal_event_resets_barrier(self):
        pool = self._recovery_pool()
        reset = threading.Event()
        pool.board.iq.rearm_signal_capture = reset.set
        pool._signal_capture_rejected("Signal event 187 incomplete when event 188 completed")
        self.assertTrue(reset.wait(1.0))


if __name__ == "__main__":
    unittest.main()
