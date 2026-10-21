"""Burst reception from a controller's reference input."""

from collections import deque
import math
import threading
import time
import numpy as np
from .iq_tx_control import _rpc


class ReferenceMonitor:
    def __init__(self, board):
        self.board, self.host = board, board.host
        self.latest, self.error = None, None
        self.changed = threading.Condition()
        self.bursts = deque(maxlen=24)
        self.key, self.parts = None, {}
        self.subscription = board.subscribe_controller_messages(0x314D5149, self.receive)

    def receive(self, data):
        if len(data) < 64:
            return
        h = np.frombuffer(data[:64], dtype="<u4")
        total, offset, count = map(int, h[7:10])
        if not (256 <= total <= 8188 and count == min(256, total - offset) and offset % 256 == 0 and 0 < count and offset + count <= total and len(data) == 64 + 4 * count):
            return
        key = tuple(h[[1, 2, 12, 13]])
        if self.key != key:
            self.key, self.parts = key, {}
        w = np.frombuffer(data[64:], dtype="<u4").astype(np.int32)
        i, q = w & 1023, (w >> 10) & 1023
        # Controller ADC Q has opposite polarity to conventional complex IQ.
        self.parts[offset] = np.where(i >= 512, i - 1024, i) - 1j * np.where(q >= 512, q - 1024, q)
        if len(self.parts) == math.ceil(total / 256):
            with self.changed:
                self.latest = np.concatenate([self.parts[k] for k in sorted(self.parts)])
                self.bursts.append((int(h[2]), int(h[1]), int(h[3]), self.latest))
                self.changed.notify_all()
            self.parts = {}

    def collect(self, count=6, timeout=10):
        """Wait for complete, fresh bursts from the current monitor configuration."""
        config = _rpc(self.board, "get_iq_monitor")
        if not config["enabled"]:
            raise RuntimeError("Reference monitor is disabled")
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                frames = [z for generation, capture, rate, z in self.bursts if generation == config["generation"] and rate == 80000000 and 0 < (capture - config["captures"]) % (1 << 32) < (1 << 31)]
                if len(frames) >= count:
                    return frames[:count]
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("Controller monitor capture timed out")
                self.changed.wait(left)

    def close(self):
        self.board.unsubscribe_controller_messages(self.subscription)
