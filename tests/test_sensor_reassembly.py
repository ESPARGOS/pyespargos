import struct

import pytest

from espargos.sensor import SENSOR_PACKET_TYPE_HEADER, SensorFragment, SensorMessageReassembler, SensorPacket


def test_out_of_order_duplicates_and_sliding_expiry():
    reader = SensorMessageReassembler(timeout_s=1)
    frag = SensorFragment
    reader.push([frag(1, 2, 3, b"c"), frag(2, 1, 2, b"old")], now=0)
    assert not reader.push([frag(1, 2, 3, b"C")], now=0.75)
    assert not reader.push([frag(1, 0, 3, b"A"), frag(2, 0, 2, b"new")], now=1.5)
    assert [m.payload for m in reader.push([frag(1, 1, 3, b"B")], now=1.5)] == [b"ABC"]
    assert [m.payload for m in reader.push([frag(2, 0, 1, b"single")], now=2)] == [b"single"]
    assert not reader.push([frag(2, 1, 2, b"late")], now=2)
    reader.clear()
    assert not reader.push([frag(2, 0, 2, b"next session")], now=2)


def test_packet_ownership_and_terminator_are_checked_before_dispatch():
    def header(uid, size=0):
        return struct.pack("<IHBB", uid, size, 0, 1)

    packet = struct.pack("<I", SENSOR_PACKET_TYPE_HEADER) + header(1, 1) + b"a"
    assert SensorPacket.from_bytes(packet + bytes(8)).fragments[0].payload == b"a"
    with pytest.raises(ValueError, match="terminator"):
        SensorPacket.from_bytes(packet)
    with pytest.raises(ValueError, match="multiple sensors"):
        SensorPacket.from_bytes(packet + header(1 << 29 | 1) + bytes(8))
