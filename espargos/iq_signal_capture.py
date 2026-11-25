"""Assemble complete Signal IQ captures from one ESPARGOS array.

Signal mode captures an event across the eight antennas of one array. Separate
arrays trigger independently, so their events cannot be assumed to match even
when they share a clock and reference. The firmware always exports
the complete 56-chunk bank in which threshold detection ran and may append a
configured prefix of the next bank. Every chunk repeats the event's total
length and carries a common ``capture_id`` plus its offset. Those fields are
the association key used while collecting the event. The synchronized raw SRAM
``source_chunk_index`` is then an independent correctness check: offset 0 must
name the same global chunk on all eight sensors and every later offset must
advance contiguously from it.

Nothing is emitted until every offset contains every sensor, all event metadata
agrees, and each sensor's raw source indices advance contiguously through the
event. A missing, conflicting, malformed, or unsynchronised part rejects the
whole event; this module never publishes a shortened intersection.
"""

from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

from .iq_cluster import CHUNK_SAMPLES
from .iq_packet import (
    IQ_CHUNK_FLAG_SIGNAL_CAPTURE,
    IQ_CHUNK_FLAG_SIGNAL_FIRST,
    IQ_CHUNK_FLAG_SIGNAL_LAST,
    IQ_SYNC_INFO_GRID_SYNCED,
)

__all__ = [
    "IQ_SIGNAL_BANK_CHUNKS",
    "IQ_SIGNAL_MAX_CAPTURE_CHUNKS",
    "IQSignalAssembler",
    "IQSignalCapture",
]

IQ_SIGNAL_BANK_CHUNKS = 56
IQ_SIGNAL_MAX_CAPTURE_CHUNKS = 112
_INVALID_CAPTURE_ID = 0xFFFFFFFF
_SOURCE_INDEX_MASK = 0x00FFFFFF
_SIGNAL_FLAG_MASK = IQ_CHUNK_FLAG_SIGNAL_CAPTURE | IQ_CHUNK_FLAG_SIGNAL_FIRST | IQ_CHUNK_FLAG_SIGNAL_LAST


@dataclass(frozen=True)
class IQSignalCapture:
    """One complete configurable-length capture from every sensor in one array.

    ``iq`` and ``sample_rx_gain`` have shape
    ``(board, row, column, capture_sample)``. ``source_chunk_start`` and
    ``source_chunk_end`` retain each sensor's raw 24-bit grid range for
    diagnostics; a published capture always has the same range on every sensor.
    """

    capture_id: int
    sample_rate_hz: int
    center_freq_hz: int
    config_generation: np.ndarray
    fire_time_ns: np.ndarray
    sync_info: int
    source_chunk_start: np.ndarray
    source_chunk_end: np.ndarray
    iq: np.ndarray
    sample_rx_gain: np.ndarray

    @property
    def chunk_count(self) -> int:
        return self.iq.shape[-1] // CHUNK_SAMPLES

    @property
    def duration_seconds(self) -> float:
        return self.iq.shape[-1] / self.sample_rate_hz


@dataclass
class _PendingEvent:
    generation: int
    capture_id: int
    chunk_count: int
    serial: int
    chunks: dict = field(default_factory=dict)


class IQSignalAssembler:
    """Assemble exact Signal events and reject incomplete events as a unit."""

    def __init__(self, max_pending_events=4, on_rejected_event=None):
        self.max_pending_events = max(2, int(max_pending_events))
        self._on_rejected_event = on_rejected_event
        self._events = {}
        self._finished = OrderedDict()
        self._active_generation = None
        self._serial = 0
        self.completed_captures = 0
        self.dropped_captures = 0
        self.last_drop_reason = None
        self.drop_reasons = {}

    def clear(self):
        self._events.clear()
        self._finished.clear()
        self._active_generation = None

    @staticmethod
    def _forward(new, old):
        delta = (int(new) - int(old)) & 0xFFFFFFFF
        return 0 < delta < 0x80000000

    def _record_drop(self, reason):
        self.dropped_captures += 1
        self.last_drop_reason = reason
        self.drop_reasons[reason] = self.drop_reasons.get(reason, 0) + 1

    def _remember_finished(self, key):
        self._finished[key] = None
        self._finished.move_to_end(key)
        while len(self._finished) > 64:
            self._finished.popitem(last=False)

    def _drop_event(self, key, reason):
        if key in self._finished:
            return
        event = self._events.pop(key, None)
        self._remember_finished(key)
        self._record_drop(reason)
        # Rejection is a host-side delivery decision, just like acceptance.
        # Retire the exact sensor snapshot after recording the failure so one
        # bad/UDP-lost event cannot exhaust the bounded outstanding-event
        # window and stop otherwise valid captures.
        if event is not None and self._on_rejected_event is not None:
            self._on_rejected_event(event.generation, event.capture_id, reason)

    @staticmethod
    def _identity(cluster):
        completed = np.asarray(cluster.completion, dtype=bool)
        complete = completed.all()
        if not completed.size or (not complete and not completed.any()):
            return None

        def present(field):
            values = np.asarray(field, dtype=np.uint32)
            return values if complete else values[completed]

        if not np.all(present(cluster.flags) & IQ_CHUNK_FLAG_SIGNAL_CAPTURE):
            return None
        identity = []
        for field in (cluster.config_generation, cluster.capture_id, cluster.capture_chunk_offset, cluster.capture_chunk_count):
            values = present(field)
            first = int(values.flat[0])
            if not (values == first).all():
                return None
            identity.append(first)
        generation, capture_id, offset, chunk_count = identity
        if capture_id == _INVALID_CAPTURE_ID or not IQ_SIGNAL_BANK_CHUNKS <= chunk_count <= IQ_SIGNAL_MAX_CAPTURE_CHUNKS or not 0 <= offset < chunk_count:
            return None
        return generation, capture_id, offset, chunk_count

    @staticmethod
    def _copy_position(target, source, position):
        target._iq[position] = source.iq[position]
        target._sample_rx_gain[position] = source.sample_rx_gain[position]
        target._flags[position] = source.flags[position]
        target._sample_rate_hz[position] = source.sample_rate_hz[position]
        target._center_freq_hz[position] = source.center_freq_hz[position]
        target._config_generation[position] = source.config_generation[position]
        target._sync_info[position] = source.sync_info[position]
        target._fire_time_ns[position] = source.fire_time_ns[position]
        target._dropped_chunks[position] = source.dropped_chunks[position]
        target._source_chunk_index[position] = source.source_chunk_index[position]
        target._capture_id[position] = source.capture_id[position]
        target._capture_chunk_offset[position] = source.capture_chunk_offset[position]
        target._capture_chunk_count[position] = source.capture_chunk_count[position]
        target._mark_sensor_position_complete(position)

    @staticmethod
    def _same_position(a, b, position):
        return (
            np.array_equal(a.iq[position], b.iq[position])
            and np.array_equal(a.sample_rx_gain[position], b.sample_rx_gain[position])
            and int(a.flags[position]) == int(b.flags[position])
            and int(a.sample_rate_hz[position]) == int(b.sample_rate_hz[position])
            and int(a.center_freq_hz[position]) == int(b.center_freq_hz[position])
            and int(a.config_generation[position]) == int(b.config_generation[position])
            and int(a.sync_info[position]) == int(b.sync_info[position])
            and int(a.fire_time_ns[position]) == int(b.fire_time_ns[position])
            and int(a.source_chunk_index[position]) == int(b.source_chunk_index[position])
            and int(a.capture_id[position]) == int(b.capture_id[position])
            and int(a.capture_chunk_offset[position]) == int(b.capture_chunk_offset[position])
            and int(a.capture_chunk_count[position]) == int(b.capture_chunk_count[position])
        )

    def _merge(self, aggregate, incoming):
        for position in zip(*np.nonzero(np.asarray(incoming.completion, dtype=bool))):
            if aggregate.completion[position]:
                if not self._same_position(aggregate, incoming, position):
                    return False
            else:
                self._copy_position(aggregate, incoming, position)
        return True

    def _select_generation(self, generation):
        if self._active_generation is None:
            self._active_generation = generation
            return True
        if generation == self._active_generation:
            return True
        if not self._forward(generation, self._active_generation):
            return False
        for key in list(self._events):
            self._drop_event(key, "configuration changed before Signal event completed")
        self._active_generation = generation
        return True

    def add_cluster(self, cluster):
        """Consume one IQ chunk cluster and return zero or one full capture."""

        identity = self._identity(cluster)
        if identity is None:
            # Ordinary interval/accumulation traffic is intentionally ignored.
            completed = np.asarray(cluster.completion, dtype=bool)
            flags = np.asarray(cluster.flags, dtype=np.uint32)
            if np.any(completed & ((flags & IQ_CHUNK_FLAG_SIGNAL_CAPTURE) != 0)):
                self._record_drop("malformed Signal chunk identity")
            return []
        generation, capture_id, offset, chunk_count = identity
        if not self._select_generation(generation):
            return []
        key = (generation, capture_id)
        if key in self._finished:
            return []

        event = self._events.get(key)
        if event is None:
            self._serial += 1
            event = _PendingEvent(generation, capture_id, chunk_count, self._serial)
            self._events[key] = event
        elif event.chunk_count != chunk_count:
            self._drop_event(
                key,
                f"Signal event {capture_id} changed length from " f"{event.chunk_count} to {chunk_count}",
            )
            return []
        aggregate = event.chunks.get(offset)
        if aggregate is None:
            event.chunks[offset] = cluster
        elif not self._merge(aggregate, cluster):
            self._drop_event(key, f"conflicting duplicate in Signal event {capture_id}, offset {offset}")
            return []

        # A settled partial chunk cluster has exhausted the pool's straggler
        # window. It can never become an 8/8 event. Retire it now (and
        # acknowledge this exact generation/capture ID) instead of waiting
        # for a later complete event: the firmware cannot acquire that later
        # event while the shared BOOT backpressure barrier remains low.
        if cluster.settled and not cluster.is_complete:
            self._drop_event(
                key,
                f"settled incomplete sensor set in Signal event " f"{capture_id}, offset {offset}",
            )
            return []

        capture = self._finish_if_complete(key)
        if capture is None:
            self._trim()
            return []

        # Completion of a later hardware event proves that older incomplete
        # events cannot become publishable without crossing an event boundary.
        for other_key, other in list(self._events.items()):
            if other.generation == generation and self._forward(capture_id, other.capture_id):
                self._drop_event(
                    other_key,
                    f"Signal event {other.capture_id} incomplete when event {capture_id} completed",
                )
        return [capture]

    def _finish_if_complete(self, key):
        event = self._events[key]
        if len(event.chunks) != event.chunk_count:
            return None
        if any(offset not in event.chunks or not event.chunks[offset].is_complete for offset in range(event.chunk_count)):
            return None

        run = [event.chunks[offset] for offset in range(event.chunk_count)]
        first = run[0]
        sensor_shape = first.completion.shape
        offsets = np.arange(event.chunk_count, dtype=np.uint32).reshape((-1,) + (1,) * len(sensor_shape))
        sample_rate = int(first.sample_rate_hz.flat[0])
        center_freq = int(first.center_freq_hz.flat[0])
        sync_info = int(first.sync_info.flat[0])
        fire_times = first.fire_time_ns.copy()
        source_start = first.source_chunk_index & _SOURCE_INDEX_MASK

        if not np.all(source_start == source_start.flat[0]):
            self._drop_event(key, f"array source epoch mismatch in Signal event {event.capture_id}")
            return None
        if not (sync_info & IQ_SYNC_INFO_GRID_SYNCED):
            self._drop_event(key, f"Signal event {event.capture_id} is not grid-synchronised")
            return None

        # Check each field across the whole event before copying its samples.
        # The common generation and capture ID do not establish that the grid
        # stayed continuous or that each sensor kept its original fire epoch.
        def field(name):
            return np.stack([getattr(chunk, name) for chunk in run])

        def mismatch(actual, expected, reason):
            wrong = actual != expected
            if wrong.any():
                offset = int(np.nonzero(wrong)[0][0])
                self._drop_event(key, f"{reason} in Signal event {event.capture_id}, offset {offset}")
                return True
            return False

        for name, expected in (
            ("sample_rate_hz", sample_rate),
            ("center_freq_hz", center_freq),
            ("sync_info", sync_info),
            ("config_generation", event.generation),
            ("capture_id", event.capture_id),
            ("capture_chunk_offset", offsets),
            ("capture_chunk_count", event.chunk_count),
        ):
            if mismatch(field(name), expected, "metadata mismatch"):
                return None
        if mismatch(field("fire_time_ns"), fire_times, "per-sensor metadata changed"):
            return None
        flags = np.full_like(offsets, IQ_CHUNK_FLAG_SIGNAL_CAPTURE)
        flags[0] |= IQ_CHUNK_FLAG_SIGNAL_FIRST
        flags[-1] |= IQ_CHUNK_FLAG_SIGNAL_LAST
        if mismatch(field("flags") & _SIGNAL_FLAG_MASK, flags, "bad boundary markers"):
            return None
        if mismatch(field("source_chunk_index") & _SOURCE_INDEX_MASK, (source_start + offsets) & _SOURCE_INDEX_MASK, "non-contiguous sensor source indices"):
            return None

        capture = IQSignalCapture(
            capture_id=event.capture_id,
            sample_rate_hz=sample_rate,
            center_freq_hz=center_freq,
            config_generation=first.config_generation.copy(),
            fire_time_ns=fire_times,
            sync_info=sync_info,
            source_chunk_start=source_start.copy().reshape(sensor_shape),
            source_chunk_end=((source_start + event.chunk_count - 1) & _SOURCE_INDEX_MASK).astype(np.uint32).reshape(sensor_shape),
            iq=np.concatenate([part.iq for part in run], axis=-1),
            sample_rx_gain=np.concatenate([part.sample_rx_gain for part in run], axis=-1),
        )
        self._events.pop(key, None)
        self._remember_finished(key)
        self.completed_captures += 1
        return capture

    def _trim(self):
        while len(self._events) > self.max_pending_events:
            key, _event = min(self._events.items(), key=lambda item: item[1].serial)
            self._drop_event(key, "Signal event exceeded pending-event window")
