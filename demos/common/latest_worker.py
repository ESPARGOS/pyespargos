"""Bounded latest-per-source processing for live demos, independent of Qt."""

from collections import OrderedDict, deque
import logging
from threading import Condition, Thread


class LatestWorker:
    """Keep the newest pending items per key (one by default).

    The callback runs outside the submitting thread. Keys are served fairly in
    arrival order, so a busy source cannot starve another selected source.
    """

    def __init__(self, process, *, max_sources=128):
        self._process = process
        self._limit = max_sources
        self._pending = OrderedDict()
        self._condition = Condition()
        self._closed = False
        self._thread = Thread(target=self._run, name="latest-measurement", daemon=True)
        self._thread.start()

    def submit(self, key, value, *, keep=1):
        if keep < 1:
            raise ValueError("keep must be positive")
        with self._condition:
            if self._closed:
                return
            pending = self._pending.setdefault(key, deque())
            pending.append(value)
            while len(pending) > keep:
                pending.popleft()
            if len(self._pending) > self._limit:
                self._pending.popitem(last=False)
            self._condition.notify()

    def clear(self):
        with self._condition:
            self._pending.clear()

    def close(self):
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._condition.notify()
        self._thread.join()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending)
                if self._closed:
                    return
                key, pending = self._pending.popitem(last=False)
                value = pending.popleft()
                if pending:
                    self._pending[key] = pending
            try:
                self._process(value)
            except Exception:
                logging.getLogger(__name__).exception("Live measurement processing failed")
