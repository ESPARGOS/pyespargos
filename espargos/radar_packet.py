"""Parse the transmit-side report produced for an active radar frame.

In radar mode, ESPARGOS sensors take turns transmitting scheduled Wi-Fi frames
while the other sensors measure their CSI.  The receive-side CSI identifies the
frame, but cannot describe what happened inside the transmitting radio: whether
transmission succeeded, which antenna feed and power were used, or precisely
when the frame left the transmitter.

The transmitting sensor therefore emits a separate radar TX report.  Its MAC
addresses and sequence control associate it with the receivers' CSI reports,
while its transmit counter, RF-switch state, power, status, and timestamps
describe the transmit event.  In particular, the optional hardware timestamp
supports transmit-time phase correction and the status fields help verify and
diagnose radar schedules.
"""

import binascii

from .sensor import RFSwitchState
from .wifi import SequenceControl

RADAR_TX_REPORT_TYPE_HEADER = 0x52545852

RADAR_TX_REPORT_TLV_TYPE_FRAME_META = 1
RADAR_TX_REPORT_TLV_TYPE_TIMING_META = 2
RADAR_TX_REPORT_TLV_TYPE_RADAR_META = 3
RADAR_TX_REPORT_TLV_TYPE_TX_META = 4
RADAR_TX_REPORT_TLV_TYPE_RAW_META = 5
RADAR_TX_REPORT_TLV_TYPE_CRC32 = 255

RADAR_TX_REPORT_FLAG_HAS_HW_TIMESTAMP = 1 << 0


class RadarTxReportPacket:
    def __init__(self, buf=None):
        raw = bytes(buf if buf is not None else b"")
        if len(raw) < 4:
            raise ValueError("Radar TX report TLV packet too short")

        self._raw = raw
        self.type_header = int.from_bytes(raw[0:4], byteorder="little")
        if self.type_header != RADAR_TX_REPORT_TYPE_HEADER:
            raise ValueError("Unexpected radar TX report type header")

        self.source_mac = bytes(6)
        self.dest_mac = bytes(6)
        self.seq_ctrl = SequenceControl(b"\x00\x00")
        self.frame_len = 0
        self.software_enqueue_timestamp_us = 0
        self.tx_count = 0
        self.rfswitch_state = RFSwitchState.SENSOR_RFSWITCH_UNKNOWN
        self.tx_power = -1
        self.flags = 0
        self.tx_status = 0
        self.ifidx = 0
        self.descriptor_slot = 0xFF
        self.txdesc_word0 = 0
        self.txdesc_word4 = 0
        self.txdesc_word8 = 0
        self.txdesc_word10 = 0
        self.timestamp_reg0 = 0
        self.timestamp_reg1 = 0
        self.timestamp_reg2 = 0
        self.crc32 = None
        self._crc_valid = False

        offset = 4
        while offset < len(raw):
            if offset + 3 > len(raw):
                raise ValueError("Malformed radar TX report TLV header")

            tlv_type = raw[offset]
            tlv_len = int.from_bytes(raw[offset + 1 : offset + 3], byteorder="little")
            tlv_start = offset
            offset += 3
            tlv_end = offset + tlv_len
            if tlv_end > len(raw):
                raise ValueError("Malformed radar TX report TLV length")

            value = raw[offset:tlv_end]

            if tlv_type == RADAR_TX_REPORT_TLV_TYPE_FRAME_META:
                if tlv_len < 16:
                    raise ValueError("Invalid radar TX report frame meta TLV")
                self.source_mac = bytes(value[0:6])
                self.dest_mac = bytes(value[6:12])
                self.seq_ctrl = SequenceControl(value[12:14])
                self.frame_len = int.from_bytes(value[14:16], byteorder="little")
            elif tlv_type == RADAR_TX_REPORT_TLV_TYPE_TIMING_META:
                if tlv_len < 8:
                    raise ValueError("Invalid radar TX report timing meta TLV")
                self.software_enqueue_timestamp_us = int.from_bytes(value[0:8], byteorder="little")
            elif tlv_type == RADAR_TX_REPORT_TLV_TYPE_RADAR_META:
                if tlv_len < 8:
                    raise ValueError("Invalid radar TX report radar meta TLV")
                self.tx_count = int.from_bytes(value[0:4], byteorder="little")
                self.rfswitch_state = value[4]
                self.tx_power = value[5]
            elif tlv_type == RADAR_TX_REPORT_TLV_TYPE_TX_META:
                if tlv_len < 8:
                    raise ValueError("Invalid radar TX report TX meta TLV")
                self.flags = int.from_bytes(value[0:2], byteorder="little")
                self.tx_status = value[2]
                self.ifidx = value[3]
                self.descriptor_slot = value[4]
            elif tlv_type == RADAR_TX_REPORT_TLV_TYPE_RAW_META:
                if tlv_len < 28:
                    raise ValueError("Invalid radar TX report raw meta TLV")
                self.txdesc_word0 = int.from_bytes(value[0:4], byteorder="little")
                self.txdesc_word4 = int.from_bytes(value[4:8], byteorder="little")
                self.txdesc_word8 = int.from_bytes(value[8:12], byteorder="little")
                self.txdesc_word10 = int.from_bytes(value[12:16], byteorder="little")
                self.timestamp_reg0 = int.from_bytes(value[16:20], byteorder="little")
                self.timestamp_reg1 = int.from_bytes(value[20:24], byteorder="little")
                self.timestamp_reg2 = int.from_bytes(value[24:28], byteorder="little")
            elif tlv_type == RADAR_TX_REPORT_TLV_TYPE_CRC32:
                if tlv_len != 4:
                    raise ValueError("Invalid radar TX report CRC32 TLV")
                if tlv_end != len(raw):
                    raise ValueError("Radar TX report CRC32 TLV must be last")
                self.crc32 = int.from_bytes(value, byteorder="little")
                computed_crc = binascii.crc32(raw[:tlv_start]) & 0xFFFFFFFF
                if computed_crc != self.crc32:
                    raise ValueError(f"Radar TX report TLV CRC32 mismatch " f"(expected 0x{self.crc32:08x}, computed 0x{computed_crc:08x})")
                self._crc_valid = True
            offset = tlv_end

        if not self._crc_valid:
            raise ValueError("Radar TX report TLV CRC32 missing")

    def __bytes__(self):
        return self._raw

    @property
    def tx_succeeded(self):
        return self.tx_status != 0

    @property
    def is_retry(self):
        """Radar TX reports describe the initially enqueued transmission."""

        return False

    @property
    def has_hardware_tx_timestamp(self):
        return bool(self.flags & RADAR_TX_REPORT_FLAG_HAS_HW_TIMESTAMP)

    def get_hardware_tx_timestamp_ns(self) -> float:
        """Decode the raw ESP32-C61 TX timestamp into sensor-local nanoseconds."""

        if not self.has_hardware_tx_timestamp:
            return float("nan")

        raw = (((int(self.timestamp_reg0) * 80) + (int(self.timestamp_reg1) & 0x7F)) - 640) << 3
        return float(raw) * 1.5625

    def get_hardware_tx_phase_raw(self) -> int:
        """Extract the apparent signed 11-bit phase field."""

        phase = (int(self.timestamp_reg2) >> 7) & 0x7FF
        if phase & 0x400:
            phase = 0x800 - phase
        return phase
