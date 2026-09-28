"""Fail-closed assembly of wired-consensus Signal IQ captures.

Signal mode is different from the ordinary raw-IQ stream. Pulling the shared
BOOT net low creates one array-wide capture event. The firmware always exports
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
        if not np.any(completed):
            return None
        flags = np.asarray(cluster.flags, dtype=np.uint32)[completed]
        if np.any((flags & IQ_CHUNK_FLAG_SIGNAL_CAPTURE) == 0):
            return None
        generations = np.unique(np.asarray(cluster.config_generation, dtype=np.uint32)[completed])
        capture_ids = np.unique(np.asarray(cluster.capture_id, dtype=np.uint32)[completed])
        offsets = np.unique(np.asarray(cluster.capture_chunk_offset, dtype=np.uint32)[completed])
        if generations.size != 1 or capture_ids.size != 1 or offsets.size != 1:
            return None
        capture_id = int(capture_ids[0])
        offset = int(offsets[0])
        chunk_counts = np.unique(np.asarray(cluster.capture_chunk_count, dtype=np.uint32)[completed])
        if chunk_counts.size != 1:
            return None
        chunk_count = int(chunk_counts[0])
        if capture_id == _INVALID_CAPTURE_ID or not IQ_SIGNAL_BANK_CHUNKS <= chunk_count <= IQ_SIGNAL_MAX_CAPTURE_CHUNKS or not 0 <= offset < chunk_count:
            return None
        return int(generations[0]), capture_id, offset, chunk_count

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
        sensor_shape = np.asarray(run[0].completion).shape
        sample_rate = None
        center_freq = None
        sync_info = None
        generations = None
        fire_times = None
        source_start = None

        for offset, cluster in enumerate(run):
            fields = (
                np.asarray(cluster.sample_rate_hz),
                np.asarray(cluster.center_freq_hz),
                np.asarray(cluster.sync_info),
                np.asarray(cluster.config_generation),
                np.asarray(cluster.capture_id),
                np.asarray(cluster.capture_chunk_offset),
                np.asarray(cluster.capture_chunk_count),
            )
            expected_scalars = (
                sample_rate,
                center_freq,
                sync_info,
                event.generation,
                event.capture_id,
                offset,
                event.chunk_count,
            )
            scalar_values = []
            for field_value, expected in zip(fields, expected_scalars):
                unique = np.unique(field_value)
                if unique.size != 1 or (expected is not None and int(unique[0]) != expected):
                    self._drop_event(
                        key,
                        f"metadata mismatch in Signal event {event.capture_id}, offset {offset}",
                    )
                    return None
                scalar_values.append(int(unique[0]))
            if offset == 0:
                sample_rate, center_freq, sync_info = scalar_values[:3]
                generations = np.asarray(cluster.config_generation, dtype=np.uint32).copy()
                fire_times = np.asarray(cluster.fire_time_ns, dtype=np.uint64).copy()
                source_start = np.asarray(cluster.source_chunk_index, dtype=np.uint32) & _SOURCE_INDEX_MASK
                if np.unique(source_start).size != 1:
                    self._drop_event(
                        key,
                        f"array source epoch mismatch in Signal event {event.capture_id}",
                    )
                    return None
            elif not np.array_equal(cluster.config_generation, generations) or not np.array_equal(cluster.fire_time_ns, fire_times):
                self._drop_event(
                    key,
                    f"per-sensor metadata changed inside Signal event {event.capture_id}",
                )
                return None

            if not (sync_info & IQ_SYNC_INFO_GRID_SYNCED):
                self._drop_event(key, f"Signal event {event.capture_id} is not grid-synchronised")
                return None

            expected_flags = IQ_CHUNK_FLAG_SIGNAL_CAPTURE
            if offset == 0:
                expected_flags |= IQ_CHUNK_FLAG_SIGNAL_FIRST
            if offset == event.chunk_count - 1:
                expected_flags |= IQ_CHUNK_FLAG_SIGNAL_LAST
            actual_flags = np.asarray(cluster.flags, dtype=np.uint32) & _SIGNAL_FLAG_MASK
            if np.any(actual_flags != expected_flags):
                self._drop_event(
                    key,
                    f"bad boundary markers in Signal event {event.capture_id}, offset {offset}",
                )
                return None

            expected_source = (source_start + offset) & _SOURCE_INDEX_MASK
            actual_source = np.asarray(cluster.source_chunk_index, dtype=np.uint32) & _SOURCE_INDEX_MASK
            if not np.array_equal(actual_source, expected_source):
                self._drop_event(
                    key,
                    f"non-contiguous sensor source indices in Signal event {event.capture_id}, offset {offset}",
                )
                return None

        capture = IQSignalCapture(
            capture_id=event.capture_id,
            sample_rate_hz=sample_rate,
            center_freq_hz=center_freq,
            config_generation=generations,
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
