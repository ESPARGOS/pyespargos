"""Burst reception from a controller's reference input."""

from collections import deque
from operator import index
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
        self.parts = {}
        self.closed = False
        self.subscription = board.subscribe_controller_messages(0x314D5149, self.receive)

    def receive(self, data):
        if len(data) < 64:
            return
        h = np.frombuffer(data[:64], dtype="<u4")
        total, offset, count = map(int, h[7:10])
        if not (256 <= total <= 8188 and count == min(256, total - offset) and offset % 256 == 0 and 0 < count and offset + count <= total and len(data) == 64 + 4 * count):
            return
        key = tuple(h[[1, 2, 3, 7, 12, 13]])
        # UDP fragments from successive captures can arrive interleaved.
        if key not in self.parts and len(self.parts) == 4:
            del self.parts[next(iter(self.parts))]
        parts = self.parts.setdefault(key, {})
        w = np.frombuffer(data[64:], dtype="<u4").astype(np.int32)
        i, q = w & 1023, (w >> 10) & 1023
        # Controller ADC Q has opposite polarity to conventional complex IQ.
        parts[offset] = np.where(i >= 512, i - 1024, i) - 1j * np.where(q >= 512, q - 1024, q)
        if len(parts) == math.ceil(total / 256):
            with self.changed:
                self.latest = np.concatenate([parts[k] for k in sorted(parts)])
                self.bursts.append((int(h[2]), int(h[1]), int(h[3]), self.latest))
                self.changed.notify_all()
            del self.parts[key]

    def collect(self, count=6, timeout=10):
        """Wait for complete, fresh bursts from the current monitor configuration."""
        count = index(count)
        if count < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("count and timeout must be positive")
        config = _rpc(self.board, "iq/monitor")
        if not config["enabled"]:
            raise RuntimeError("Reference monitor is disabled")
        deadline = time.monotonic() + timeout
        frames = []
        last = config["captures"]
        with self.changed:
            while not self.closed:
                for generation, capture, rate, samples in self.bursts:
                    if generation != config["generation"] or rate != config["sample_rate_hz"]:
                        continue
                    if 0 < (capture - last) % (1 << 32) < (1 << 31):
                        frames.append(samples)
                        last = capture
                        if len(frames) == count:
                            return frames
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("Controller monitor capture timed out")
                self.changed.wait(left)
        raise RuntimeError("Reference monitor was closed")

    def close(self):
        self.board.unsubscribe_controller_messages(self.subscription)
        with self.changed:
            self.closed = True
            self.changed.notify_all()
