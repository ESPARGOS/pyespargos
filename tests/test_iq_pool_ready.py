import logging
import threading
import unittest
from types import SimpleNamespace

from espargos.iq_receiver import IQReceiver


def _snapshot(count=3, uptime=1000, wifi_count=20, missing=None):
    return {
        "sensors": [
            {
                "antid": antenna_id,
                "uptime_ms": uptime,
                "iq_timed_syncs": count,
                "wifi_promisc_cb": wifi_count,
            }
            for antenna_id in range(8)
            if antenna_id != missing
        ]
    }


class _Control:
    def __init__(self, replies):
        self.replies = iter(replies)

    def get_json(self, path):
        assert path == "get_sensor_debug"
        return next(self.replies)


class _Board:
    host = "test-board"

    def __init__(self, replies):
        self.control = _Control(replies)


def _pool(board):
    pool = object.__new__(IQReceiver)
    pool.boards = [board]
    return pool


class IQPoolReadyTests(unittest.TestCase):
    @staticmethod
    def _recovery_pool():
        pool = object.__new__(IQReceiver)
        pool._signal_recovery_lock = threading.Lock()
        pool._signal_epoch_mismatch_streak = 0
        pool._signal_recovery_running = False
        pool._logger = logging.getLogger("test.iq_signal_recovery")
        pool.boards = [SimpleNamespace(iq=SimpleNamespace())]
        return pool

    def test_state_requires_every_sensor(self):
        board = _Board([_snapshot(missing=7)])
        self.assertIsNone(IQReceiver._sensor_sync_state(board))

    def test_wifi_state_requires_every_sensor(self):
        board = _Board([_snapshot(missing=4)])
        self.assertIsNone(IQReceiver._sensor_wifi_state(board))

    def test_waits_for_fresh_wifi_rx_on_every_sensor(self):
        old = IQReceiver._sensor_wifi_state(_Board([_snapshot()]))
        pool = _pool(_Board([_snapshot(), _snapshot(wifi_count=21)]))
        pool._wait_for_fresh_wifi_rx([old], [True], timeout=0.5)

    def test_wifi_rx_after_reboot_is_fresh(self):
        old = IQReceiver._sensor_wifi_state(_Board([_snapshot(uptime=9000, wifi_count=500)]))
        pool = _pool(_Board([_snapshot(uptime=20, wifi_count=1)]))
        pool._wait_for_fresh_wifi_rx([old], [True], timeout=0.1)

    def test_unchanged_wifi_rx_times_out(self):
        old = IQReceiver._sensor_wifi_state(_Board([_snapshot()]))
        pool = _pool(_Board([_snapshot()] * 20))
        with self.assertRaisesRegex(RuntimeError, "ant0"):
            pool._wait_for_fresh_wifi_rx([old], [True], timeout=0.02)

    def test_waits_for_every_counter_to_advance(self):
        old = IQReceiver._sensor_sync_state(_Board([_snapshot()]))
        replies = [_snapshot(), _snapshot(count=4)]
        pool = _pool(_Board(replies))
        pool._wait_for_fresh_iq_sync([old], [True], timeout=0.5, stability_time=0)

    def test_sensor_reboot_accepts_first_new_grid_fire(self):
        old = IQReceiver._sensor_sync_state(_Board([_snapshot(count=99, uptime=9000)]))
        pool = _pool(_Board([_snapshot(count=1, uptime=20)]))
        pool._wait_for_fresh_iq_sync([old], [True], timeout=0.1, stability_time=0)

    def test_counter_reset_is_not_progress_until_uptime_reports_reboot(self):
        old = IQReceiver._sensor_sync_state(_Board([_snapshot(count=99, uptime=9000)]))
        pool = _pool(_Board([_snapshot(count=0, uptime=9001)] * 20))
        with self.assertRaisesRegex(RuntimeError, "ant0"):
            pool._wait_for_fresh_iq_sync([old], [True], timeout=0.02, stability_time=0)

    def test_unchanged_counter_times_out(self):
        old = IQReceiver._sensor_sync_state(_Board([_snapshot()]))
        # Supply enough unchanged snapshots for the short polling window.
        pool = _pool(_Board([_snapshot()] * 20))
        with self.assertRaisesRegex(RuntimeError, "ant0"):
            pool._wait_for_fresh_iq_sync([old], [True], timeout=0.02)

    def test_consecutive_signal_epoch_mismatches_reset_barrier_without_resync(self):
        pool = self._recovery_pool()
        called = threading.Event()
        calls = []

        def sync():
            calls.append(True)
            called.set()

        pool.sync = sync
        rearmed = []
        pool.boards[0].iq.rearm_signal_capture = lambda: rearmed.append(True)
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
        pool.boards[0].iq.rearm_signal_capture = reset.set
        pool._signal_capture_rejected("settled incomplete sensor set in Signal event 7, offset 0")
        self.assertTrue(reset.wait(1.0))
        self.assertFalse(synced.is_set())

    def test_superseded_incomplete_signal_event_resets_barrier(self):
        pool = self._recovery_pool()
        reset = threading.Event()
        pool.boards[0].iq.rearm_signal_capture = reset.set
        pool._signal_capture_rejected("Signal event 187 incomplete when event 188 completed")
        self.assertTrue(reset.wait(1.0))


if __name__ == "__main__":
    unittest.main()
