import struct
import zlib

import numpy as np
import pytest

from espargos.iq_packet import IQAccumPacket


def make_packet(count=4, flags=0):
    i_sum = np.arange(256, dtype=np.int32) * count
    q_sum = -np.arange(256, dtype=np.int32) * count
    interleaved = np.column_stack((i_sum, q_sum)).astype("<i4").tobytes()
    words = [
        7,  # sequence
        9,  # window index
        4096,
        8192,
        2,  # vector chunks
        1,  # vector section
        2048,
        count,
        0x12345678,
        1,
        80_000_000,
        2_437_000_000,
        60,
        1,
        11,
        flags,
        3,
        2,
        0x89ABCDEF,
        0x01234567,
        0x00010001,
    ]
    body = b"IQA1" + struct.pack("<21I", *words) + interleaved
    return body + struct.pack("<I", zlib.crc32(body))


def test_accumulation_packet_decodes_exact_sums_and_normalized_iq():
    packet = IQAccumPacket(make_packet(count=4, flags=3))
    assert packet.vector_chunks == 2
    assert packet.vector_chunk_index == 1
    assert packet.accumulated_chunks == 4
    assert packet.is_partial
    assert packet.deadline_aborted
    np.testing.assert_array_equal(packet.i_sum, np.arange(256, dtype=np.int32) * 4)
    np.testing.assert_array_equal(packet.q_sum, -np.arange(256, dtype=np.int32) * 4)
    expected = np.arange(256, dtype=np.float32) * (1 + 1j)
    np.testing.assert_allclose(packet.decode_iq(), expected)


def test_accumulation_packet_rejects_corrupt_crc():
    data = bytearray(make_packet())
    data[100] ^= 1
    with pytest.raises(ValueError, match="CRC"):
        IQAccumPacket(data)
