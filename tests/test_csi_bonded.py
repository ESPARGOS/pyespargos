"""Both encodings of a bonded HT packet use the same RF subcarrier grid."""

import binascii
import ctypes
import struct

import numpy as np
import pytest

from espargos import csi_compression, csi_packet
from espargos.csi_cluster import CSICluster
from espargos.revisions import BoardRevisionDensiflorus
from espargos.sensor import SensorMessage
from espargos.wifi import WiFiFrameKey


@pytest.mark.parametrize("secondary", [1, 2])
@pytest.mark.parametrize("compressed", [False, True])
def test_ht40_primary_channel(secondary, compressed):
    ctrl = csi_packet.WiFiPacketRxControlV3(bytes(ctypes.sizeof(csi_packet.WiFiPacketRxControlV3)))
    ctrl.he_siga1 = 0x80
    ctrl.cur_bb_format = csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_HT
    ctrl.second = secondary
    ctrl.rx_channel_estimate_len = 234
    ctrl.rxstart_time_cyc = 64  # Together with the timestamp below, zero timing correction.
    if compressed:
        taps = np.arange(32, dtype="<i2") - 15
        payload = bytes([12]) + taps.tobytes()
        expected = csi_compression.decode_compressed_ht40(payload)
    else:
        pairs = np.arange(-117, 117, dtype=np.int8).reshape(-1, 2)
        payload = pairs.tobytes()
        samples = pairs[:, 1].astype(float) + 1j * pairs[:, 0]
        expected = np.r_[-1j * samples[:57], np.zeros(3), -samples[-57:]]

    def tlv(kind, data):
        return struct.pack("<BH", kind, len(data)) + data

    wire = struct.pack("<I", csi_packet.CSI_TYPE_HEADER)
    wire += tlv(3, struct.pack("<Q", 20)) + tlv(5, bytes(ctrl)) + tlv(7 if compressed else 6, payload)
    packet = csi_packet.CSIPacket(wire + tlv(255, struct.pack("<I", binascii.crc32(wire))))
    cluster = CSICluster(WiFiFrameKey.from_packet(packet), [BoardRevisionDensiflorus()])
    cluster.add_message(0, SensorMessage(uid=0, antenna_id=0, payload=packet))

    np.testing.assert_allclose(cluster.deserialize_csi_ht40ltf()[cluster.completion][0], expected)
    primary = cluster.deserialize_csi_ht20ltf()
    np.testing.assert_allclose(primary[cluster.completion][0], np.r_[0, 0, expected[:55]] if secondary == 1 else np.r_[expected[-55:], 0, 0])
    assert np.isnan(primary[~cluster.completion]).all()
