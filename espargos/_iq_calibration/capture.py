"""Collect IQ chunks for calibration."""

import time
from collections import deque

from ..iq_tx_control import TXStatus
from ..iq_packet import IQChunkPacket, IQ_CHUNK_TYPE_HEADER, IQ_CHUNK_FLAG_REFERENCE, IQ_CHUNK_FLAG_REFERENCE_READY


class _Captures(TXStatus):
    """Collect consecutive chunks from selected sensors."""

    def __init__(self, boards):
        super().__init__(boards)
        self.ids = set()
        self.pending = {}
        self.frames = {}
        self.chunks = 1
        self.generations = {}
        self.not_before = {}
        try:
            for bi, b in enumerate(boards):
                self.subscriptions.append((b, b.subscribe_sensor_messages(IQ_CHUNK_TYPE_HEADER, lambda m, bi=bi: self.receive(8 * bi + m.antenna_id, m))))
        except Exception:
            self.close()
            raise

    def receive(self, sensor, message):
        if sensor not in self.ids:
            return
        try:
            packet = IQChunkPacket(message.payload)
        except ValueError:
            return
        if packet.flags & (IQ_CHUNK_FLAG_REFERENCE | IQ_CHUNK_FLAG_REFERENCE_READY):
            return
        with self.changed:
            if sensor not in self.ids or (sensor in self.generations and packet.config_generation != self.generations[sensor]):
                return
            # Another board may still send buffered data from before the pilot,
            # without a change in configuration generation.
            captured_ns = packet.fire_time_ns + round(packet.chunk_counter * 256e9 / packet.sample_rate_hz)
            if sensor in self.not_before:
                # The hardware chunk counter wraps after 2**24 chunks.
                span_ns = round((1 << 24) * 256e9 / packet.sample_rate_hz)
                age = (captured_ns - self.not_before[sensor] + span_ns // 2) % span_ns - span_ns // 2
                if age < 0:
                    return
            index = packet.source_chunk_index & 0xFFFFFF
            key = (packet.fire_time_ns, packet.config_generation, index // self.chunks)
            pending = self.pending.setdefault(sensor, {})
            pending.setdefault(key, {})[index % self.chunks] = packet
            if len(pending[key]) == self.chunks:
                self.frames[sensor].append([pending[key][i] for i in range(self.chunks)])
                del pending[key]
                self.changed.notify_all()
            while len(pending) > 4:
                del pending[next(iter(pending))]

    def collect(self, ids, samples, count, timeout, generations=None, not_before=None):
        if not ids:
            return {}
        deadline = time.monotonic() + timeout
        with self.changed:
            self.ids = set(ids)
            self.generations = generations or {}
            self.not_before = not_before or {}
            self.chunks = samples // 256
            self.pending = {}
            self.frames = {i: deque(maxlen=count) for i in ids}
            try:
                while any(len(v) < count for v in self.frames.values()):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        missing = [i for i, v in self.frames.items() if len(v) < count]
                        raise RuntimeError(f"Missing calibration captures from sensors {missing}")
                    self.changed.wait(left)
                return {i: list(v) for i, v in self.frames.items()}
            finally:
                self.ids = set()


def _grid_origin(packet):
    base = (packet.source_chunk_index - packet.chunk_counter) & 0xFFFFFF
    return packet.fire_time_ns - round(base * 256e9 / packet.sample_rate_hz)
