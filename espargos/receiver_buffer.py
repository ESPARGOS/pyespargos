"""Nonblocking producer buffering with explicit retention and loss accounting."""

from collections import deque
from dataclasses import dataclass
from threading import Condition
import math
import time


@dataclass(frozen=True)
class BufferPolicy:
    """Limits pending reports, independently of cluster and recording storage.

    None capacity opts into unbounded recording. max_age uses host arrival time.
    Overflow drops the oldest or newest report and is always counted. Producers
    never block the shared transport. batch_size bounds work per receiver turn.
    """

    capacity: int | None = 4096
    max_age: float | None = None
    overflow: str = "oldest"
    batch_size: int = 256

    def __post_init__(self):
        if self.capacity is not None and (type(self.capacity) is not int or self.capacity < 1):
            raise ValueError("Buffer capacity must be positive or None")
        if self.max_age is not None and (not math.isfinite(self.max_age) or self.max_age <= 0):
            raise ValueError("Buffer maximum age must be positive or None")
        if self.overflow not in ("oldest", "newest"):
            raise ValueError("Buffer overflow must be oldest or newest")
        if type(self.batch_size) is not int or self.batch_size < 1:
            raise ValueError("Buffer batch size must be positive")


@dataclass(frozen=True)
class BufferStats:
    pending: int
    received: int
    dropped: int
    expired: int
    high_watermark: int


class ReceiverBuffer:
    def __init__(self, policy=BufferPolicy(), *, clock=time.monotonic):
        self.policy = policy
        self._clock = clock
        self._condition = Condition()
        self._items = deque()
        self._received = self._dropped = self._expired = self._high_watermark = 0
        self._closed = False

    def _expire(self):
        if self.policy.max_age is not None:
            cutoff = self._clock() - self.policy.max_age
            while self._items and self._items[0][0] <= cutoff:
                self._items.popleft()
                self._expired += 1

    def configure(self, policy):
        if not isinstance(policy, BufferPolicy):
            raise TypeError("Expected BufferPolicy")
        with self._condition:
            self.policy = policy
            self._expire()
            while policy.capacity is not None and len(self._items) > policy.capacity:
                (self._items.popleft if policy.overflow == "oldest" else self._items.pop)()
                self._dropped += 1
            self._condition.notify_all()

    def put_nowait(self, value):
        with self._condition:
            if self._closed:
                return
            self._received += 1
            self._expire()
            if self.policy.capacity is not None and len(self._items) >= self.policy.capacity:
                self._dropped += 1
                if self.policy.overflow == "newest":
                    return
                self._items.popleft()
            self._items.append((self._clock(), value))
            self._high_watermark = max(self._high_watermark, len(self._items))
            self._condition.notify()

    def take(self, timeout=0):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                self._expire()
                if self._items or self._closed:
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return []
                self._condition.wait(remaining)
            return [self._items.popleft()[1] for _ in range(min(len(self._items), self.policy.batch_size))]

    @property
    def stats(self):
        with self._condition:
            self._expire()
            return BufferStats(len(self._items), self._received, self._dropped, self._expired, self._high_watermark)

    def qsize(self):
        return self.stats.pending

    def close(self):
        with self._condition:
            self._closed = True
            self._items.clear()
            self._condition.notify_all()
