import struct
import zlib
from types import SimpleNamespace

import numpy as np
import pytest

from espargos.iq_receiver import IQReceiver
from espargos.iq_rx_stream import IQStreamBlock, IQStreamPacket


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
