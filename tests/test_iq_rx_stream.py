import struct
import zlib
from types import SimpleNamespace

import numpy as np
import pytest

from espargos.iq_receiver import IQReceiver
from espargos.iq_rx_stream import IQStreamBlock, IQStreamPacket
from espargos.calibration import CalibrationState


def record(generation=7, grid=3, fire=110000000):
    # Provide a wire fixture independent of the parser, including a 64-bit index.
    header = struct.pack("<4sIIIQQQI", b"IRS1", generation, grid, 2437000000,
                         2**32 + 128, fire, 100000000, 16000)
    iq = np.tile(np.array([32, -64], dtype="<i2"), 128)
    body = header + iq.tobytes()
    return body + struct.pack("<I", zlib.crc32(body))


def test_stream_wire_and_corruption():
    data = record()
    p = IQStreamPacket(data)
    assert p.first_sample == 2**32 + 128
    np.testing.assert_array_equal(p.decode_iq(), np.full(128, 1 - 2j))
    damaged = bytearray(data)
    damaged[45] ^= 1
    for malformed in (damaged, data[1:], data + b"\x00"):
        with pytest.raises(ValueError):
            IQStreamPacket(malformed)


def test_array_alignment_requires_a_common_grid():
    receiver = SimpleNamespace(boards=[None, None])
    def key(board, antenna, p):
        return IQReceiver._get_cluster_key(receiver, board, SimpleNamespace(payload=p, antenna_id=antenna))
    a = IQStreamPacket(record())
    assert key(0, 0, a) != key(0, 1, a)
    assert key(0, 0, a) != key(0, 0, IQStreamPacket(record(fire=42)))
    grid = (1 << 18) | (1 << 16) | 7
    a = IQStreamPacket(record(grid=grid))
    b = IQStreamPacket(record(generation=5, grid=grid, fire=450000000))
    assert key(0, 0, a) == key(1, 7, b)
    assert key(0, 0, a) != key(1, 7, IQStreamPacket(record(grid=grid+1)))
    revision = SimpleNamespace(antenna_id_to_row_col=lambda n: divmod(n, 4))
    cluster = IQStreamBlock(a, [revision, revision])
    for board, antenna, packet in ((0, 0, a), (1, 7, b)):
        cluster.add_message(board, SimpleNamespace(payload=packet, antenna_id=antenna))
    assert cluster.synchronized and cluster.completion.sum() == 2
    assert np.isnan(cluster.iq[~cluster.completion]).all()
    assert cluster.fire_time_ns[1, 1, 3] == b.fire_time_ns


def test_delayed_reports_do_not_redeliver_an_array_block():
    receiver = IQReceiver(SimpleNamespace(boards=[]), chunk_settle_timeout=0)
    receiver.board_revisions = (SimpleNamespace(antenna_id_to_row_col=lambda n: divmod(n, 4)),)
    receiver._expected = np.ones((1, 2, 4), dtype=bool)
    received = []
    receiver.add_stream_callback(received.append, include_partial=True)
    packet = IQStreamPacket(record(grid=(1 << 18) | (1 << 16) | 7))
    reports = [(0, SimpleNamespace(antenna_id=a, payload=packet)) for a in range(8)]
    for batch in (reports, reports[:3], reports):
        for message in batch:
            receiver._input_queue.put_nowait(message)
        receiver._run(timeout=0)
    assert len(received) == 1
    assert received[0].is_complete


def test_calibration_follows_each_present_antenna_on_a_partial_grid():
    revision = SimpleNamespace(antenna_id_to_row_col=lambda n: divmod(n, 4))
    packet = IQStreamPacket(record(grid=(1 << 18) | (1 << 16) | 7))
    block = IQStreamBlock(packet, [revision])
    epochs = np.full(block.shape, packet.fire_time_ns, dtype=np.uint64)
    model = SimpleNamespace(phase_offsets=np.zeros(block.shape))
    receiver = SimpleNamespace(_calibration=model, _calibration_epoch=epochs.tobytes(),
                               _calibration_sample_rate=16000, _calibration_center_frequency=2437000000)
    assert not IQReceiver.calibration_applies_to(receiver, block)
    block.add_message(0, SimpleNamespace(antenna_id=2, payload=packet))
    assert IQReceiver.calibration_applies_to(receiver, block)
    state = CalibrationState.from_iq_packet("array", 2, packet, boot_id=4)
    assert (state.epoch, state.sample_origin_ns) == (packet.fire_time_ns, packet.grid_origin_ns)
    model.phase_offsets[0, 0, 2] = np.nan
    assert not IQReceiver.calibration_applies_to(receiver, block)
    model.phase_offsets.fill(0)
    block.fire_time_ns[0, 0, 2] += 1
    assert not IQReceiver.calibration_applies_to(receiver, block)
    with pytest.raises(ValueError, match="synchronized"):
        CalibrationState.from_iq_packet("array", 2, IQStreamPacket(record()), boot_id=4)
