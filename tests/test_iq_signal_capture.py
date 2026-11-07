from types import SimpleNamespace

import numpy as np

from espargos.iq_cluster import IQCluster
from espargos.iq_packet import (
    IQ_CHUNK_FLAG_SIGNAL_CAPTURE,
    IQ_CHUNK_FLAG_SIGNAL_FIRST,
    IQ_CHUNK_FLAG_SIGNAL_LAST,
    IQ_SYNC_INFO_GRID_SYNCED,
)
from espargos.iq_signal_capture import (
    IQ_SIGNAL_BANK_CHUNKS,
    IQ_SIGNAL_MAX_CAPTURE_CHUNKS,
    IQSignalAssembler,
)


class _Revision:
    @staticmethod
    def antenna_id_to_row_col(antenna_id):
        return divmod(antenna_id, 4)


_BASES = np.full(8, 1000)
_DEFAULT_CAPTURE_CHUNKS = IQ_SIGNAL_MAX_CAPTURE_CHUNKS


def _flags(offset, chunk_count):
    value = IQ_CHUNK_FLAG_SIGNAL_CAPTURE
    if offset == 0:
        value |= IQ_CHUNK_FLAG_SIGNAL_FIRST
    if offset == chunk_count - 1:
        value |= IQ_CHUNK_FLAG_SIGNAL_LAST
    return value


def _payload(
    antenna_id,
    capture_id,
    offset,
    *,
    generation=7,
    bases=_BASES,
    chunk_count=_DEFAULT_CAPTURE_CHUNKS,
):
    value = capture_id * 1000 + offset * 10 + antenna_id
    return SimpleNamespace(
        flags=_flags(offset, chunk_count),
        sample_rate_hz=80_000_000,
        center_freq_hz=2_462_000_000,
        config_generation=generation,
        sync_info=IQ_SYNC_INFO_GRID_SYNCED | 4,
        fire_time_ns=123456 + antenna_id * 1000,
        chunk_counter=int(bases[antenna_id]) + offset,
        source_chunk_index=(int(bases[antenna_id]) + offset) & 0xFFFFFF,
        capture_id=capture_id,
        capture_chunk_offset=offset,
        capture_chunk_count=chunk_count,
        dropped_chunks=0,
        decode_iq=lambda: np.full(256, value + 1j * value, np.complex64),
        sample_rx_gain=lambda: np.full(256, 60, np.uint8),
    )


def _cluster(
    capture_id,
    offset,
    *,
    missing=(),
    generation=7,
    bases=_BASES,
    chunk_count=_DEFAULT_CAPTURE_CHUNKS,
):
    cluster = IQCluster((capture_id << 32) | offset, [_Revision()])
    for antenna_id in range(8):
        if antenna_id in missing:
            continue
        cluster.add_message(
            0,
            SimpleNamespace(
                antenna_id=antenna_id,
                payload=_payload(
                    antenna_id,
                    capture_id,
                    offset,
                    generation=generation,
                    bases=bases,
                    chunk_count=chunk_count,
                ),
            ),
        )
    return cluster


def _feed_event(
    assembler,
    capture_id,
    *,
    order=None,
    generation=7,
    missing_at=None,
    chunk_count=_DEFAULT_CAPTURE_CHUNKS,
):
    output = []
    offsets = range(chunk_count) if order is None else order
    for offset in offsets:
        missing = () if missing_at is None or offset != missing_at[0] else missing_at[1]
        output.extend(
            assembler.add_cluster(
                _cluster(
                    capture_id,
                    offset,
                    missing=missing,
                    generation=generation,
                    chunk_count=chunk_count,
                )
            )
        )
    return output


def test_exact_event_assembles_in_offset_order_despite_arrival_reordering():
    assembler = IQSignalAssembler()
    order = list(range(_DEFAULT_CAPTURE_CHUNKS))[::-1]
    output = _feed_event(assembler, 3, order=order)

    assert len(output) == 1
    capture = output[0]
    assert capture.capture_id == 3
    assert capture.chunk_count == _DEFAULT_CAPTURE_CHUNKS
    assert capture.iq.shape == (1, 2, 4, _DEFAULT_CAPTURE_CHUNKS * 256)
    assert capture.duration_seconds == _DEFAULT_CAPTURE_CHUNKS * 256 / 80_000_000
    np.testing.assert_array_equal(capture.source_chunk_start.flat, _BASES)
    np.testing.assert_array_equal(
        capture.source_chunk_end.flat,
        (_BASES + _DEFAULT_CAPTURE_CHUNKS - 1) & 0xFFFFFF,
    )
    assert np.unique(capture.source_chunk_start).size == 1
    np.testing.assert_array_equal(capture.iq[0, 1, 3, :256], 3007 + 3007j)


def test_one_missing_sensor_never_publishes_partial_and_is_retired_by_next_event():
    rejected = []
    assembler = IQSignalAssembler(on_rejected_event=lambda generation, capture_id, reason: rejected.append((generation, capture_id, reason)))
    assert not _feed_event(assembler, 10, missing_at=(57, (7,)))
    assert assembler.completed_captures == 0

    output = _feed_event(assembler, 11)
    assert [capture.capture_id for capture in output] == [11]
    assert assembler.completed_captures == 1
    assert assembler.dropped_captures == 1
    assert "incomplete" in assembler.last_drop_reason
    assert len(rejected) == 1
    assert rejected[0][:2] == (7, 10)
    assert "incomplete" in rejected[0][2]


def test_settled_partial_retires_event_without_waiting_for_a_later_event():
    rejected = []
    assembler = IQSignalAssembler(on_rejected_event=lambda generation, capture_id, reason: rejected.append((generation, capture_id, reason)))
    partial = _cluster(12, 0, missing=(7,))
    partial.settled = True

    assert not assembler.add_cluster(partial)
    assert assembler.completed_captures == 0
    assert assembler.dropped_captures == 1
    assert rejected[0][:2] == (7, 12)
    assert "settled incomplete sensor set" in rejected[0][2]
    assert not assembler._events


def test_late_complementary_partials_merge_without_weakening_completeness():
    assembler = IQSignalAssembler()
    output = []
    for offset in range(_DEFAULT_CAPTURE_CHUNKS):
        if offset == 50:
            output.extend(assembler.add_cluster(_cluster(20, offset, missing=(7,))))
            output.extend(assembler.add_cluster(_cluster(20, offset, missing=tuple(range(7)))))
        else:
            output.extend(assembler.add_cluster(_cluster(20, offset)))
    assert [capture.capture_id for capture in output] == [20]
    assert assembler.dropped_captures == 0


def test_metadata_change_rejects_whole_event():
    assembler = IQSignalAssembler()
    for offset in range(_DEFAULT_CAPTURE_CHUNKS):
        cluster = _cluster(30, offset)
        if offset == 40:
            cluster._fire_time_ns[0, 0, 0] += 1
        assembler.add_cluster(cluster)
    assert assembler.completed_captures == 0
    assert assembler.dropped_captures == 1
    assert "metadata changed" in assembler.last_drop_reason


def test_bad_boundary_marker_rejects_whole_event():
    assembler = IQSignalAssembler()
    for offset in range(_DEFAULT_CAPTURE_CHUNKS):
        cluster = _cluster(40, offset)
        if offset == 1:
            cluster._flags[:] |= IQ_CHUNK_FLAG_SIGNAL_FIRST
        assembler.add_cluster(cluster)
    assert assembler.completed_captures == 0
    assert assembler.dropped_captures == 1
    assert "boundary markers" in assembler.last_drop_reason


def test_noncontiguous_source_index_rejects_whole_event():
    assembler = IQSignalAssembler()
    for offset in range(_DEFAULT_CAPTURE_CHUNKS):
        cluster = _cluster(50, offset)
        if offset == 70:
            cluster._source_chunk_index[0, 1, 0] += 1
        assembler.add_cluster(cluster)
    assert assembler.completed_captures == 0
    assert assembler.dropped_captures == 1
    assert "non-contiguous" in assembler.last_drop_reason


def test_different_sensor_source_epoch_rejects_whole_event():
    bases = np.full(8, 1000)
    bases[6] -= 3 * 56
    assembler = IQSignalAssembler()
    output = []
    for offset in range(_DEFAULT_CAPTURE_CHUNKS):
        output.extend(assembler.add_cluster(_cluster(55, offset, bases=bases)))
    assert not output
    assert assembler.completed_captures == 0
    assert assembler.dropped_captures == 1
    assert "source epoch mismatch" in assembler.last_drop_reason


def test_unsynchronised_event_is_rejected():
    assembler = IQSignalAssembler()
    for offset in range(_DEFAULT_CAPTURE_CHUNKS):
        cluster = _cluster(60, offset)
        cluster._sync_info[:] = 0
        assembler.add_cluster(cluster)
    assert assembler.completed_captures == 0
    assert assembler.dropped_captures == 1
    assert "not grid-synchronised" in assembler.last_drop_reason


def test_new_configuration_retires_old_event_and_late_old_chunks_are_ignored():
    assembler = IQSignalAssembler()
    for offset in range(20):
        assembler.add_cluster(_cluster(70, offset, generation=7))

    output = _feed_event(assembler, 1, generation=8)
    assert [capture.capture_id for capture in output] == [1]
    assert assembler.dropped_captures == 1
    assert not assembler.add_cluster(_cluster(70, 20, generation=7))
    assert assembler._active_generation == 8


def test_source_index_wrap_is_contiguous_and_duplicate_event_is_not_reemitted():
    bases = np.full(8, 0xFFFFE0)
    assembler = IQSignalAssembler()
    output = []
    for offset in range(_DEFAULT_CAPTURE_CHUNKS):
        output.extend(assembler.add_cluster(_cluster(80, offset, bases=bases)))
    assert [capture.capture_id for capture in output] == [80]
    assert np.all(output[0].source_chunk_end == 0x4F)
    assert not assembler.add_cluster(_cluster(80, 0, bases=bases))
    assert assembler.completed_captures == 1


def test_one_bank_event_assembles_at_configured_length():
    assembler = IQSignalAssembler()
    output = _feed_event(
        assembler,
        81,
        chunk_count=IQ_SIGNAL_BANK_CHUNKS,
    )
    assert [capture.chunk_count for capture in output] == [IQ_SIGNAL_BANK_CHUNKS]
    assert output[0].iq.shape[-1] == IQ_SIGNAL_BANK_CHUNKS * 256
    np.testing.assert_array_equal(
        output[0].source_chunk_end.flat,
        (_BASES + IQ_SIGNAL_BANK_CHUNKS - 1) & 0xFFFFFF,
    )


def test_event_length_mismatch_is_rejected_fail_closed():
    assembler = IQSignalAssembler()
    assembler.add_cluster(_cluster(82, 0, chunk_count=IQ_SIGNAL_BANK_CHUNKS))
    assert not assembler.add_cluster(_cluster(82, 1, chunk_count=IQ_SIGNAL_MAX_CAPTURE_CHUNKS))
    assert assembler.dropped_captures == 1
    assert "changed length" in assembler.last_drop_reason
