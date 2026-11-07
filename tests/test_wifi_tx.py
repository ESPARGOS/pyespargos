"""WiFi transmission naming must preserve firmware packet and timing semantics."""

import binascii
import struct

import numpy as np
import pytest

from espargos import WiFiTxConfig, WiFiTxReportPacket
from espargos import wifi_tx
from espargos.csi_packet import CSIPacket, SERIALIZED_CSI_TLV_FRAME_FLAG_IS_SCHEDULED_TX
from espargos.csi_backlog import CSIBacklog


def report_bytes():
    def tlv(kind, value):
        return struct.pack("<BH", kind, len(value)) + value

    raw = struct.pack("<I", 0x52545852)
    raw += tlv(1, bytes.fromhex("726164610003ffffffffffff") + struct.pack("<HH", 0x1230, 100))
    raw += tlv(2, struct.pack("<Q", 123456))
    raw += tlv(3, struct.pack("<IBBBB", 7, 2, 34, 0, 0))
    raw += tlv(4, struct.pack("<HBBBBBB", 1, 0, 0, 2, 0, 0, 0))
    raw += tlv(5, struct.pack("<7I", 0, 0, 0, 0, 1000, 16, 0))
    return raw + tlv(255, struct.pack("<I", binascii.crc32(raw)))


def test_decode_existing_firmware_report():
    report = WiFiTxReportPacket(report_bytes())
    assert report.source_mac.hex() == "726164610003"
    assert report.tx_count == 7
    assert report.rf_switch_state == 2
    assert report.tx_power == 34
    assert report.has_hardware_tx_timestamp
    assert report.get_hardware_tx_timestamp_ns() == ((1000 * 80 + 16 - 640) << 3) * 1.5625


def test_report_still_rejects_bad_crc():
    raw = bytearray(report_bytes())
    raw[-1] ^= 1
    with pytest.raises(ValueError, match="CRC32 mismatch"):
        WiFiTxReportPacket(raw)


def test_scheduled_flag_retains_wire_value():
    packet = CSIPacket.__new__(CSIPacket)
    assert SERIALIZED_CSI_TLV_FRAME_FLAG_IS_SCHEDULED_TX == 2
    packet.frame_flags = 2
    assert packet.is_scheduled_tx
    packet.frame_flags = 0
    assert not packet.is_scheduled_tx


def test_tx_timestamp_correction_uses_sensor_clock_offset():
    frequencies = np.array([-1e6, 0, 1e6])
    samples = np.ones((2, 3), dtype=np.complex64)
    corrected = wifi_tx.correct_csi_tx_timestamps(samples, [5e-6, 6e-6], [0, 1], frequencies, [1e-6, 1.5e-6])
    expected = np.exp(2j * np.pi * np.array([4e-6, 4.5e-6])[:, None] * frequencies)
    np.testing.assert_allclose(corrected, expected, atol=1e-6)
    assert corrected.dtype == samples.dtype


def test_config_can_carry_distinct_board_schedules():
    boards = [{"active_by_antid": [True] + [False] * 7}, {"active_by_antid": [False] * 8}]
    config = WiFiTxConfig(board_configs=boards)
    assert config.board_configs == boards


def test_transmit_report_attaches_to_csi_cluster_without_marking_rx_complete():
    from espargos.csi_cluster import CSICluster
    from espargos.revisions import BoardRevisionDensiflorus
    from espargos.sensor import SensorMessage
    from espargos.wifi import WiFiFrameKey

    report = WiFiTxReportPacket(report_bytes())
    revision = BoardRevisionDensiflorus()
    cluster = CSICluster(WiFiFrameKey.from_packet(report), [revision])
    message = SensorMessage(uid=3, antenna_id=3, payload=report)
    assert cluster.add_message(0, message)
    assert cluster.has_tx_report
    assert cluster.tx_report is report
    row, column = revision.antenna_id_to_row_col(3)
    assert cluster.tx_index == row * 4 + column
    assert not np.any(cluster.completion)
    assert not cluster.add_message(0, message)
