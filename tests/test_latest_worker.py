"""Live visualization must not build an unbounded history of old measurements."""
import threading
import unittest
from demos.common.latest_worker import LatestWorker


class LatestWorkerTests(unittest.TestCase):
    def test_replaces_pending_work_and_serves_other_sources(self):
        started, release, finished = (threading.Event() for _ in range(3))
        seen = []

        def process(value):
            if value == "first":
                started.set()
                release.wait(2)
            seen.append(value)
            if len(seen) == 3:
                finished.set()

        worker = LatestWorker(process)
        try:
            worker.submit("a", "first")
            self.assertTrue(started.wait(1))
            worker.submit("a", "old")
            worker.submit("b", "other")
            worker.submit("a", "newest")
            release.set()
            self.assertTrue(finished.wait(1))
        finally:
            release.set()
            worker.close()
        self.assertEqual(seen, ["first", "newest", "other"])

    def test_clear_discards_pending_measurements(self):
        started, release = threading.Event(), threading.Event()
        seen = []

        def process(value):
            started.set()
            release.wait(2)
            seen.append(value)

        worker = LatestWorker(process)
        try:
            worker.submit("a", 1)
            self.assertTrue(started.wait(1))
            worker.submit("a", 2)
            worker.clear()
        finally:
            release.set()
            worker.close()
        self.assertEqual(seen, [1])
