from enum import IntEnum
import ctypes
import binascii
import numpy as np

from . import constants

# Internal constants
_ESPARGOS_SPI_BUFFER_SIZE_V3 = 384
SPI_TYPE_HEADER_CSI = 0xE4CD0BAC
SPI_TYPE_HEADER_RADAR_TX_REPORT = 0x52545852
SPI_TYPE_HEADER_JUMBO_FRAME = 0xDECAFBAD
JUMBO_FRAGMENT_TERMINATOR_UID = 0
CSISTREAM_UID_SENSOR_SHIFT = 29
CSISTREAM_UID_SENSOR_MASK = 0x7

# Other constants
HT_COEFFICIENTS_PER_CHANNEL = 57
"Number of channel coefficients (active subcarriers) per Wi-Fi channel in HT mode (HT-LTF)"

LEGACY_COEFFICIENTS_PER_CHANNEL = 53
"Number of channel coefficients (active subcarriers) per Wi-Fi channel in legacy mode (L-LTF)"

HE20_COEFFICIENTS_PER_CHANNEL = 245
"Number of channel coefficients (active plus invalid subcarriers) for a 20 MHz HE-LTF"

HT40_GAP_SUBCARRIERS = 3
"Gap between primary and secondary channel in HT40 mode, in subcarriers"

COMPRESSED_LLTF_FFT_SIZE = 64
COMPRESSED_HT20_FFT_SIZE = 64
COMPRESSED_HT40_FFT_SIZE = 128
COMPRESSED_HE20_FFT_SIZE = 256
COMPRESSED_LLTF_FIX32_SHIFT = 17
COMPRESSED_HT20_FIX32_SHIFT = 21
COMPRESSED_HT40_FIX32_SHIFT = 21
COMPRESSED_HE20_FIX32_SHIFT = 21
COMPRESSED_TAP_COUNT = 16
COMPRESSED_LLTF_TAP_START = 27
COMPRESSED_HT20_TAP_START = 27
COMPRESSED_HT40_TAP_START = 55
COMPRESSED_HE20_TAP_START = 120
HT20_SIMULATION_MODES = (
    "float",
    "float_corrected",
    "sc32",
    "sc32_corrected",
    "fix32",
    "fix32_corrected",
)

SERIALIZED_CSI_TLV_TYPE_FRAME_META = 2
SERIALIZED_CSI_TLV_TYPE_TIMING_META = 3
SERIALIZED_CSI_TLV_TYPE_ACQUIRE_META = 4
SERIALIZED_CSI_TLV_TYPE_RX_CTRL_RAW = 5
SERIALIZED_CSI_TLV_TYPE_CSI_RAW = 6
SERIALIZED_CSI_TLV_TYPE_CSI_COMPRESSED = 7
SERIALIZED_CSI_TLV_TYPE_RX_CTRL_COMPRESSED = 8
SERIALIZED_CSI_TLV_TYPE_GAIN_TABLE_ENTRY = 9
SERIALIZED_CSI_TLV_TYPE_CRC32 = 255

RADAR_TX_REPORT_TLV_TYPE_FRAME_META = 1
RADAR_TX_REPORT_TLV_TYPE_TIMING_META = 2
RADAR_TX_REPORT_TLV_TYPE_RADAR_META = 3
RADAR_TX_REPORT_TLV_TYPE_TX_META = 4
RADAR_TX_REPORT_TLV_TYPE_RAW_META = 5
RADAR_TX_REPORT_TLV_TYPE_CRC32 = 255

RADAR_TX_REPORT_FLAG_HAS_HW_TIMESTAMP = 1 << 0

SERIALIZED_CSI_TLV_FRAME_FLAG_IS_CALIB = 1 << 0
SERIALIZED_CSI_TLV_FRAME_FLAG_IS_RADAR = 1 << 1
SERIALIZED_CSI_TLV_FRAME_FLAG_FIRST_WORD_INVALID = 1 << 2

SERIALIZED_CSI_TLV_ACQUIRE_FLAG_FORCE_LLTF = 1 << 0
SERIALIZED_CSI_TLV_ACQUIRE_FLAG_LLTF_BIT_MODE = 1 << 1
SERIALIZED_CSI_TLV_RX_CTRL_COMPRESSED_FLAG_IS_HT40 = 1 << 0
SERIALIZED_CSI_TLV_RX_CTRL_COMPRESSED_FLAG_CHANNEL_ESTIMATE_INFO_VLD = 1 << 1


def get_csi_format_subcarrier_count(preamble_format: str) -> int:
    if preamble_format == "lltf":
        return LEGACY_COEFFICIENTS_PER_CHANNEL
    if preamble_format == "ht20":
        return HT_COEFFICIENTS_PER_CHANNEL
    if preamble_format == "ht40":
        return 2 * HT_COEFFICIENTS_PER_CHANNEL + HT40_GAP_SUBCARRIERS
    if preamble_format == "he20":
        return HE20_COEFFICIENTS_PER_CHANNEL
    raise ValueError(f"Unknown CSI preamble format: {preamble_format}")


def get_csi_format_subcarrier_indices(preamble_format: str) -> np.ndarray:
    if preamble_format == "lltf":
        return np.arange(-26, 27, dtype=np.int32)
    if preamble_format == "ht20":
        return np.arange(-28, 29, dtype=np.int32)
    if preamble_format == "ht40":
        return np.arange(-58, 59, dtype=np.int32)
    if preamble_format == "he20":
        return np.arange(-122, 123, dtype=np.int32)
    raise ValueError(f"Unknown CSI preamble format: {preamble_format}")


#####################################################
#       Enums used by multiple PHY versions         #
#####################################################
class wifi_rx_bb_format_t(IntEnum):
    RX_BB_FORMAT_11B = 0
    RX_BB_FORMAT_11G = 1
    RX_BB_FORMAT_11A = 1  # Same value as 11G
    RX_BB_FORMAT_HT = 2
    RX_BB_FORMAT_VHT = 3
    RX_BB_FORMAT_HE_SU = 4
    RX_BB_FORMAT_HE_MU = 5
    RX_BB_FORMAT_HE_ERSU = 6
    RX_BB_FORMAT_HE_TB = 7


class rfswitch_state_t(IntEnum):
    SENSOR_RFSWITCH_ISOLATION = 0
    SENSOR_RFSWITCH_REFERENCE = 1
    SENSOR_RFSWITCH_ANTENNA_R = 2
    SENSOR_RFSWITCH_ANTENNA_L = 3
    SENSOR_RFSWITCH_ANTENNA_RANDOM = 4
    SENSOR_RFSWITCH_UNKNOWN = 255


#####################################################
# Common C Structures used by multiple PHY versions #
#####################################################
class seq_ctrl_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing the sequence control field of a Wi-Fi packet.

    This structure is used to store the sequence control field of a Wi-Fi packet, which contains the fragment number and the segment number.
    """

    _pack_ = 1
    _fields_ = [("frag", ctypes.c_uint16, 4), ("seg", ctypes.c_uint16, 12)]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass


########################################################################
# C Structures for Espressif PHY version 3 (e.g., ESP32-C5, ESP32-C61) #
########################################################################
class wifi_pkt_rx_ctrl_v3_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing the `wifi_pkt_rx_ctrl_t` as provided by the ESP32.
    See the related `esp-idf code <https://github.com/espressif/esp-idf/blob/master/components/esp_wifi/include/esp_wifi_he_types.h>`_ for details.

    Variant for Espressif PHY version 3.
    """

    _pack_ = 1

    _fields_ = [
        ("rssi", ctypes.c_uint32, 8),
        ("rate", ctypes.c_uint32, 5),
        ("_reserved1", ctypes.c_uint32, 1),
        ("rate_type", ctypes.c_uint32, 2),
        ("_reserved3", ctypes.c_uint32, 12),
        ("rxmatch0", ctypes.c_uint32, 1),
        ("rxmatch1", ctypes.c_uint32, 1),
        ("rxmatch2", ctypes.c_uint32, 1),
        ("rxmatch3", ctypes.c_uint32, 1),
        ("he_siga1", ctypes.c_uint32, 32),
        ("rxend_state", ctypes.c_uint32, 8),
        ("he_siga2", ctypes.c_uint32, 16),
        ("rxstart_time_cyc", ctypes.c_uint32, 7),
        ("is_group", ctypes.c_uint32, 1),
        ("timestamp", ctypes.c_uint32, 32),
        ("cfo_low_rate", ctypes.c_uint32, 15),
        ("cfo_high_rate", ctypes.c_uint32, 15),
        ("_reserved7", ctypes.c_uint32, 2),
        ("noise_floor", ctypes.c_uint32, 8),
        ("_reserved8", ctypes.c_uint32, 8),
        ("fft_gain", ctypes.c_uint32, 8),
        ("rx_gain", ctypes.c_uint32, 8),
        ("_reserved11", ctypes.c_uint32, 8),
        ("_reserved12", ctypes.c_uint32, 8),
        ("_reserved13", ctypes.c_uint32, 2),
        ("sigb_len", ctypes.c_uint32, 10),
        ("_reserved14", ctypes.c_uint32, 1),
        ("_reserved15", ctypes.c_uint32, 1),
        ("_reserved16", ctypes.c_uint32, 1),
        ("_reserved17", ctypes.c_uint32, 1),
        ("channel", ctypes.c_uint32, 8),
        ("second", ctypes.c_uint32, 8),
        ("_reserved18", ctypes.c_uint32, 4),
        ("_reserved19", ctypes.c_uint32, 4),
        ("_reserved20", ctypes.c_uint32, 1),
        ("_reserved21", ctypes.c_uint32, 7),
        ("_reserved22", ctypes.c_uint32, 2),
        ("_reserved23", ctypes.c_uint32, 4),
        ("_reserved24", ctypes.c_uint32, 2),
        ("rxstart_time_cyc_dec", ctypes.c_uint32, 11),
        ("_reserved26", ctypes.c_uint32, 1),
        ("_reserved27", ctypes.c_uint32, 12),
        ("_reserved28", ctypes.c_uint32, 12),
        ("cur_bb_format", ctypes.c_uint32, 4),
        ("rx_channel_estimate_len", ctypes.c_uint32, 10),
        ("rx_channel_estimate_info_vld", ctypes.c_uint32, 1),
        ("_reserved29", ctypes.c_uint32, 5),
        ("_reserved30", ctypes.c_uint32, 21),
        ("_reserved31", ctypes.c_uint32, 10),
        ("_reserved32", ctypes.c_uint32, 1),
        ("_reserved33", ctypes.c_uint32, 3),
        ("_reserved34", ctypes.c_uint32, 1),
        ("_reserved35", ctypes.c_uint32, 6),
        ("_reserved36", ctypes.c_uint32, 21),
        ("_reserved37", ctypes.c_uint32, 1),
        ("_reserved38", ctypes.c_uint32, 32),
        ("_reserved39", ctypes.c_uint32, 7),
        ("_reserved40", ctypes.c_uint32, 1),
        ("_reserved41", ctypes.c_uint32, 8),
        ("_reserved42", ctypes.c_uint32, 16),
        ("sig_len", ctypes.c_uint32, 14),
        ("_reserved43", ctypes.c_uint32, 2),
        ("dump_len", ctypes.c_uint32, 14),
        ("_reserved44", ctypes.c_uint32, 2),
        ("rx_state", ctypes.c_uint32, 8),
        ("_reserved45", ctypes.c_uint32, 8),
        ("_reserved46", ctypes.c_uint32, 16),
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass


assert ctypes.sizeof(wifi_pkt_rx_ctrl_v3_t) == 64


class compressed_rx_ctrl_t(ctypes.LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ("rssi", ctypes.c_uint8),
        ("noise_floor", ctypes.c_uint8),
        ("channel", ctypes.c_uint8),
        ("secondary_channel", ctypes.c_int8),
        ("cur_bb_format", ctypes.c_uint8),
        ("rate", ctypes.c_uint8),
        ("rate_type", ctypes.c_uint8),
        ("rxstart_time_cyc", ctypes.c_uint8),
        ("rx_channel_estimate_len", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("timestamp", ctypes.c_uint32),
        ("cfo_low_rate", ctypes.c_uint16),
        ("cfo_high_rate", ctypes.c_uint16),
        ("he_sig1_mcs", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8),
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass


assert ctypes.sizeof(compressed_rx_ctrl_t) == 22


def _build_rx_ctrl_v3_from_compressed(compact_raw: bytes) -> bytes:
    compact = compressed_rx_ctrl_t(compact_raw)
    ctrl = wifi_pkt_rx_ctrl_v3_t(bytes(ctypes.sizeof(wifi_pkt_rx_ctrl_v3_t)))
    ctrl.rssi = int(compact.rssi)
    ctrl.rate = int(compact.rate)
    ctrl.rate_type = int(compact.rate_type)
    ctrl.he_siga1 = int(compact.he_sig1_mcs)
    if compact.flags & SERIALIZED_CSI_TLV_RX_CTRL_COMPRESSED_FLAG_IS_HT40:
        ctrl.he_siga1 |= 0x80
    ctrl.rxstart_time_cyc = int(compact.rxstart_time_cyc)
    ctrl.timestamp = int(compact.timestamp)
    ctrl.cfo_low_rate = int(compact.cfo_low_rate)
    ctrl.cfo_high_rate = int(compact.cfo_high_rate)
    ctrl.noise_floor = int(compact.noise_floor)
    ctrl.channel = int(compact.channel)
    if compact.secondary_channel > 0:
        ctrl.second = 1
    elif compact.secondary_channel < 0:
        ctrl.second = 2
    else:
        ctrl.second = 0
    ctrl.cur_bb_format = int(compact.cur_bb_format)
    ctrl.rx_channel_estimate_len = int(compact.rx_channel_estimate_len)
    ctrl.rx_channel_estimate_info_vld = 1 if (compact.flags & SERIALIZED_CSI_TLV_RX_CTRL_COMPRESSED_FLAG_CHANNEL_ESTIMATE_INFO_VLD) else 0
    return ctypes.string_at(ctypes.byref(ctrl), ctypes.sizeof(ctrl))


class csistream_fragment_header_t(ctypes.LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ("uid", ctypes.c_uint32),
        ("size", ctypes.c_uint16),
        ("fragment_index", ctypes.c_uint8),
        ("total_fragments", ctypes.c_uint8),
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass


assert ctypes.sizeof(csistream_fragment_header_t) == 8


# 0-56: htltf primary
# 57-59: htltf gap
# 60-116: htltf secondary
# 117-192: zeros
class csi_buf_v3_ht40_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing the CSI buffer as produced by the ESP32 PHY V3 if an HT-LTF is recorded.
    """

    _pack_ = 1
    _fields_ = [
        ("htltf_lower", ctypes.c_int8 * (HT_COEFFICIENTS_PER_CHANNEL * 2)),
        ("htltf_gap", ctypes.c_int8 * (HT40_GAP_SUBCARRIERS * 2)),  # all zeros
        ("htltf_higher", ctypes.c_int8 * (HT_COEFFICIENTS_PER_CHANNEL * 2)),
        ("reserved", ctypes.c_int8 * (11 * 2)),
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass


class csi_buf_v3_ht20_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing the CSI buffer as produced by the ESP32 PHY V3 if an HT20-LTF is recorded.
    """

    _pack_ = 1
    _fields_ = [
        ("htltf", ctypes.c_int8 * (HT_COEFFICIENTS_PER_CHANNEL * 2)),
        ("reserved", ctypes.c_int8 * (71 * 2)),
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass


# 0-53: lltf
# 54-192: zeros
class csi_buf_v3_lltf_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing the CSI buffer as produced by the ESP32 PHY V3 if an L-LTF is recorded.
    """

    _pack_ = 1
    _fields_ = [
        ("lltf", ctypes.c_int8 * (LEGACY_COEFFICIENTS_PER_CHANNEL * 2)),
        ("reserved", ctypes.c_int8 * (75 * 2)),
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass


assert ctypes.sizeof(csi_buf_v3_lltf_t) == ctypes.sizeof(csi_buf_v3_ht20_t) == ctypes.sizeof(csi_buf_v3_ht40_t) == 256


class serialized_csi_tlv_t:
    def __init__(self, buf=None):
        raw = bytes(buf if buf is not None else b"")
        if len(raw) < 4:
            raise ValueError("CSI TLV packet too short")

        self._raw = raw
        self.type_header = int.from_bytes(raw[0:4], byteorder="little")
        self.source_mac = bytes(6)
        self.dest_mac = bytes(6)
        self.seq_ctrl = seq_ctrl_t(b"\x00\x00")
        self.frame_flags = 0
        self.timestamp = 0
        self.global_timestamp_us = 0
        self.acquire_flags = 0
        self.acquire_val_scale_cfg = 0
        self.rfswitch_state = rfswitch_state_t.SENSOR_RFSWITCH_UNKNOWN
        self.gain_table_entry_raw = bytes(12)
        self.gain_table_entry_valid = False
        self.antid = 0xFF
        self.rx_ctrl = bytes()
        self.buf = bytes()
        self.csi_len = 0
        self._is_compressed = False
        self.crc32 = None
        self._crc_valid = False
        self._raw_csi_tlv = None
        self._raw_csi_padded_len = 0

        offset = 4
        while offset < len(raw):
            if offset + 3 > len(raw):
                raise ValueError("Malformed CSI TLV header")

            tlv_type = raw[offset]
            tlv_len = int.from_bytes(raw[offset + 1 : offset + 3], byteorder="little")
            tlv_start = offset
            offset += 3
            tlv_end = offset + tlv_len
            if tlv_end > len(raw):
                raise ValueError("Malformed CSI TLV length")

            value = raw[offset:tlv_end]

            if tlv_type == SERIALIZED_CSI_TLV_TYPE_FRAME_META:
                if tlv_len < 16:
                    raise ValueError("Invalid frame meta TLV")
                self.source_mac = bytes(value[0:6])
                self.dest_mac = bytes(value[6:12])
                self.seq_ctrl = seq_ctrl_t(value[12:14])
                self.frame_flags = int.from_bytes(value[14:16], byteorder="little")
            elif tlv_type == SERIALIZED_CSI_TLV_TYPE_TIMING_META:
                if tlv_len < 8:
                    raise ValueError("Invalid timing meta TLV")
                self.global_timestamp_us = int.from_bytes(value[:8], byteorder="little")
            elif tlv_type == SERIALIZED_CSI_TLV_TYPE_ACQUIRE_META:
                if tlv_len < 4:
                    raise ValueError("Invalid acquire meta TLV")
                self.acquire_flags = int.from_bytes(value[0:2], byteorder="little")
                self.acquire_val_scale_cfg = value[2]
                self.rfswitch_state = value[3]
            elif tlv_type == SERIALIZED_CSI_TLV_TYPE_GAIN_TABLE_ENTRY:
                if tlv_len < 12:
                    raise ValueError("Invalid gain table entry TLV")
                self.gain_table_entry_raw = bytes(value[:12])
                self.gain_table_entry_valid = True
            elif tlv_type == SERIALIZED_CSI_TLV_TYPE_RX_CTRL_RAW:
                self.rx_ctrl = bytes(value)
                if len(self.rx_ctrl) >= ctypes.sizeof(wifi_pkt_rx_ctrl_v3_t):
                    self.timestamp = wifi_pkt_rx_ctrl_v3_t(self.rx_ctrl).timestamp
            elif tlv_type == SERIALIZED_CSI_TLV_TYPE_RX_CTRL_COMPRESSED:
                if tlv_len < ctypes.sizeof(compressed_rx_ctrl_t):
                    raise ValueError("Invalid compressed RX CTRL TLV")
                self.rx_ctrl = _build_rx_ctrl_v3_from_compressed(bytes(value[: ctypes.sizeof(compressed_rx_ctrl_t)]))
                self.timestamp = wifi_pkt_rx_ctrl_v3_t(self.rx_ctrl).timestamp
            elif tlv_type == SERIALIZED_CSI_TLV_TYPE_CSI_RAW:
                self._raw_csi_tlv = bytes(value)
                self._raw_csi_padded_len = tlv_len
                self._is_compressed = False
            elif tlv_type == SERIALIZED_CSI_TLV_TYPE_CSI_COMPRESSED:
                self._raw_csi_tlv = bytes(value)
                self._raw_csi_padded_len = tlv_len
                self._is_compressed = True
            elif tlv_type == SERIALIZED_CSI_TLV_TYPE_CRC32:
                if tlv_len != 4:
                    raise ValueError("Invalid CRC32 TLV")
                if tlv_end != len(raw):
                    raise ValueError("CRC32 TLV must be last")
                self.crc32 = int.from_bytes(value, byteorder="little")
                computed_crc = binascii.crc32(raw[:tlv_start]) & 0xFFFFFFFF
                if computed_crc != self.crc32:
                    raise ValueError(f"CSI TLV CRC32 mismatch (expected 0x{self.crc32:08x}, computed 0x{computed_crc:08x})")
                self._crc_valid = True
            offset = tlv_end

        if not self._crc_valid:
            raise ValueError("CSI TLV CRC32 missing")
        if not self.rx_ctrl:
            raise ValueError("CSI TLV missing RX CTRL metadata")

        if self._raw_csi_tlv is not None:
            if self._is_compressed:
                logical_csi_len = min(1 + COMPRESSED_TAP_COUNT * 4, self._raw_csi_padded_len)
            else:
                logical_csi_len = min(wifi_pkt_rx_ctrl_v3_t(self.rx_ctrl).rx_channel_estimate_len, self._raw_csi_padded_len)

            self.buf = self._raw_csi_tlv[:logical_csi_len]
            self.csi_len = logical_csi_len

    def __bytes__(self):
        return self._raw

    @property
    def is_radar(self):
        return bool(self.frame_flags & SERIALIZED_CSI_TLV_FRAME_FLAG_IS_RADAR)

    @property
    def is_calib(self):
        return bool(self.frame_flags & SERIALIZED_CSI_TLV_FRAME_FLAG_IS_CALIB)

    @property
    def first_word_invalid(self):
        return bool(self.frame_flags & SERIALIZED_CSI_TLV_FRAME_FLAG_FIRST_WORD_INVALID)

    @property
    def acquire_force_lltf(self):
        return bool(self.acquire_flags & SERIALIZED_CSI_TLV_ACQUIRE_FLAG_FORCE_LLTF)

    @property
    def acquire_lltf_bit_mode(self):
        return bool(self.acquire_flags & SERIALIZED_CSI_TLV_ACQUIRE_FLAG_LLTF_BIT_MODE)

    @property
    def is_compressed(self):
        return self._is_compressed


class radar_tx_report_tlv_t:
    def __init__(self, buf=None):
        raw = bytes(buf if buf is not None else b"")
        if len(raw) < 4:
            raise ValueError("Radar TX report TLV packet too short")

        self._raw = raw
        self.type_header = int.from_bytes(raw[0:4], byteorder="little")
        if self.type_header != SPI_TYPE_HEADER_RADAR_TX_REPORT:
            raise ValueError("Unexpected radar TX report type header")

        self.source_mac = bytes(6)
        self.dest_mac = bytes(6)
        self.seq_ctrl = seq_ctrl_t(b"\x00\x00")
        self.frame_len = 0
        self.software_enqueue_timestamp_us = 0
        self.tx_count = 0
        self.rfswitch_state = rfswitch_state_t.SENSOR_RFSWITCH_UNKNOWN
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
        self.antid = 0xFF
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
                self.seq_ctrl = seq_ctrl_t(value[12:14])
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
                    raise ValueError(f"Radar TX report TLV CRC32 mismatch (expected 0x{self.crc32:08x}, computed 0x{computed_crc:08x})")
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
    def has_hardware_tx_timestamp(self):
        return bool(self.flags & RADAR_TX_REPORT_FLAG_HAS_HW_TIMESTAMP)

    def get_hardware_tx_timestamp_ns(self) -> float:
        """
        Decode the raw ESP32-C61 TX timestamp registers into sensor-local nanoseconds.

        The wire format intentionally keeps the raw register values. This helper mirrors
        the low-level recovery formula from the firmware so analysis code can choose
        whether and how to use the decoded timestamp.
        """
        if not self.has_hardware_tx_timestamp:
            return float("nan")

        raw = (((int(self.timestamp_reg0) * 80) + (int(self.timestamp_reg1) & 0x7F)) - 640) << 3
        return float(raw) * 1.5625

    def get_hardware_tx_phase_raw(self) -> int:
        """
        Extract the apparent signed 11-bit phase-ish field from timestamp register 2.
        """
        phase = (int(self.timestamp_reg2) >> 7) & 0x7FF
        if phase & 0x400:
            phase = 0x800 - phase
        return phase


def _decode_wire_complex_int8(buf, pair_count):
    values = np.frombuffer(buf[: pair_count * 2], dtype=np.int8).astype(np.float32).view(np.complex64)
    return -1.0j * np.conj(values)


def _decode_wire_complex_float32(buf, pair_count):
    values = np.frombuffer(buf[: pair_count * 8], dtype="<f4")
    return (values[0::2] + 1.0j * values[1::2]).astype(np.complex64)


def _decode_wire_complex_i16_scaled(buf, pair_count, tap_scale: float):
    right_shift = int(np.frombuffer(buf[:1], dtype=np.uint8)[0])
    values = np.frombuffer(buf[1 : 1 + pair_count * 4], dtype="<i2").astype(np.float32)
    values *= float(1 << right_shift) / tap_scale
    return (values[0::2] + 1.0j * values[1::2]).astype(np.complex64)


def unpack_lltf12_values(buf, value_count: int) -> np.ndarray:
    raw = np.frombuffer(buf[: value_count * 2], dtype=np.uint8)
    words = (raw[0::2].astype(np.uint16) | (raw[1::2].astype(np.uint16) << 8)).astype(np.uint16)
    return (((words.astype(np.int16) << 4) >> 4)).astype(np.int16)


def _ifftshift_1d(values: np.ndarray) -> np.ndarray:
    half = values.shape[-1] // 2
    return np.concatenate((values[..., half:], values[..., :half]), axis=-1)


def _fftshift_1d(values: np.ndarray) -> np.ndarray:
    half = values.shape[-1] // 2
    return np.concatenate((values[..., -half:], values[..., :-half]), axis=-1)


def _centered_ifft(values: np.ndarray) -> np.ndarray:
    return _fftshift_1d(np.fft.ifft(_ifftshift_1d(values))).astype(np.complex64)


def _centered_fft(values: np.ndarray, fft_size: int) -> np.ndarray:
    return _fftshift_1d(np.fft.fft(_ifftshift_1d(values), n=fft_size)).astype(np.complex64)


def _active_slice(fft_size: int, active_count: int) -> slice:
    start = (fft_size - active_count) // 2
    return slice(start, start + active_count)


def _build_masked_tap_correction(fft_size: int, active_count: int, tap_start: int, tap_count: int, missing_indices: list[int]) -> np.ndarray:
    window = slice(tap_start, tap_start + tap_count)
    active = _active_slice(fft_size, active_count)
    mask = np.ones((active_count,), dtype=np.complex64)
    mask[missing_indices] = 0.0
    correction = np.zeros((tap_count, tap_count), dtype=np.complex64)

    for col in range(tap_count):
        centered_cir = np.zeros((fft_size,), dtype=np.complex64)
        centered_cir[tap_start + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, fft_size)
        masked_spectrum = np.zeros((fft_size,), dtype=np.complex64)
        masked_spectrum[active] = centered_spectrum[active] * mask
        correction[:, col] = _centered_ifft(masked_spectrum)[window]

    return np.linalg.pinv(correction).astype(np.complex64)


def _clamp_s32(value: int) -> int:
    return max(min(int(value), (1 << 31) - 1), -(1 << 31))


def _reverse_bits16(x: int, order: int) -> int:
    b = x & 0xFFFF
    b = (((b & 0xFF00) >> 8) | ((b & 0x00FF) << 8)) & 0xFFFF
    b = (((b & 0xF0F0) >> 4) | ((b & 0x0F0F) << 4)) & 0xFFFF
    b = (((b & 0xCCCC) >> 2) | ((b & 0x3333) << 2)) & 0xFFFF
    b = (((b & 0xAAAA) >> 1) | ((b & 0x5555) << 1)) & 0xFFFF
    return b >> (16 - order)


def _build_sc32_twiddles(fft_size: int) -> np.ndarray:
    twiddles = np.zeros((fft_size,), dtype=np.complex128)
    for i in range(fft_size // 2):
        twiddles[i] = np.rint(np.cos(2.0 * np.pi * i / fft_size) * np.iinfo(np.int32).max) + 1.0j * np.rint(np.sin(2.0 * np.pi * i / fft_size) * np.iinfo(np.int32).max)
    return twiddles


_SC32_TWIDDLES_64 = _build_sc32_twiddles(COMPRESSED_HT20_FFT_SIZE)


def _sensor_centered_spectrum_to_ht20_observed_taps(centered_spectrum: np.ndarray) -> np.ndarray:
    fft_size = COMPRESSED_HT20_FFT_SIZE
    shift = COMPRESSED_HT20_FIX32_SHIFT
    round_const = 1 << 31
    max_s32 = np.iinfo(np.int32).max
    fft_data = np.zeros((fft_size,), dtype=np.complex128)

    for centered_index, coeff in enumerate(centered_spectrum):
        fft_index = (centered_index + fft_size // 2) % fft_size
        fft_data[fft_index] = np.rint(coeff.real * (1 << shift)) - 1.0j * np.rint(coeff.imag * (1 << shift))

    ie = 1
    n2 = fft_size // 2
    while n2 > 0:
        ia = 0
        for j in range(ie):
            cs = _SC32_TWIDDLES_64[j]
            cs_re = int(np.real(cs))
            cs_im = int(np.imag(cs))
            for _ in range(n2):
                m = ia + n2
                a_re = int(np.real(fft_data[ia]))
                a_im = int(np.imag(fft_data[ia]))
                m_re = int(np.real(fft_data[m]))
                m_im = int(np.imag(fft_data[m]))

                mul_re = cs_re * m_re + cs_im * m_im
                mul_im = cs_re * m_im - cs_im * m_re

                diff_re = _clamp_s32((((a_re * max_s32) - mul_re + round_const) >> 32))
                diff_im = _clamp_s32((((a_im * max_s32) - mul_im + round_const) >> 32))
                sum_re = _clamp_s32((((a_re * max_s32) + mul_re + round_const) >> 32))
                sum_im = _clamp_s32((((a_im * max_s32) + mul_im + round_const) >> 32))

                fft_data[m] = diff_re + 1.0j * diff_im
                fft_data[ia] = sum_re + 1.0j * sum_im
                ia += 1
            ia += n2
        ie <<= 1
        n2 >>= 1

    order = int(np.log2(fft_size))
    for i in range(1, fft_size - 1):
        j = _reverse_bits16(i, order)
        if i < j:
            fft_data[i], fft_data[j] = fft_data[j], fft_data[i]

    observed = np.zeros((COMPRESSED_TAP_COUNT,), dtype=np.complex64)
    for i in range(COMPRESSED_TAP_COUNT):
        centered_index = COMPRESSED_HT20_TAP_START + i
        fft_index = (centered_index + fft_size // 2) % fft_size
        value = fft_data[fft_index]
        observed[i] = (float(np.real(value)) - 1.0j * float(np.imag(value))) / float(1 << shift)

    return observed


def _fix32_mpy(a: int, b: int, precise_rounding: bool = False) -> int:
    mul = int(a) * int(b)
    if precise_rounding:
        mulval = mul >> 30
        tmp = mulval & 0x01
        return int((mulval >> 1) + tmp)
    return int(mul >> 32)


def _sensor_centered_spectrum_to_ht20_observed_taps_fix32(centered_spectrum: np.ndarray, precise_rounding: bool = False) -> np.ndarray:
    return _sensor_centered_spectrum_to_direct_observed_taps_fix32(
        centered_spectrum,
        COMPRESSED_HT20_FFT_SIZE,
        COMPRESSED_HT20_FIX32_SHIFT,
        COMPRESSED_HT20_TAP_START,
        precise_rounding=precise_rounding,
    )


def _sensor_centered_spectrum_to_direct_observed_taps_fix32(
    centered_spectrum: np.ndarray,
    fft_size: int,
    shift: int,
    tap_start: int,
    precise_rounding: bool = False,
) -> np.ndarray:
    scale_divisor = 8.0
    fr = np.zeros((fft_size,), dtype=np.int64)
    fi = np.zeros((fft_size,), dtype=np.int64)
    natural_input = np.conj(np.fft.ifftshift(np.asarray(centered_spectrum, dtype=np.complex64)))

    for fft_index, coeff in enumerate(natural_input):
        fr[fft_index] = int(np.rint(coeff.real * (1 << shift)))
        fi[fft_index] = _clamp_s32(-int(np.rint(coeff.imag * (1 << shift))))

    order = int(np.log2(fft_size))
    for i in range(1, fft_size):
        j = _reverse_bits16(i, order)
        if j <= i:
            continue
        fr[i], fr[j] = fr[j], fr[i]
        fi[i], fi[j] = fi[j], fi[i]

    stage = 0
    l = 1
    while l < fft_size:
        istep = l << 1
        for m in range(l):
            angle = (2.0 * np.pi * m) / istep
            wr = int(np.rint(np.cos(angle) * np.iinfo(np.int32).max))
            wi = int(np.rint(np.sin(angle) * np.iinfo(np.int32).max))

            for i in range(m, fft_size, istep):
                j = i + l
                tmpr = int(fr[j])
                tmpi = int(fi[j])

                zr = _fix32_mpy(wr, tmpr, precise_rounding) - _fix32_mpy(wi, tmpi, precise_rounding)
                zi = _fix32_mpy(wr, tmpi, precise_rounding) + _fix32_mpy(wi, tmpr, precise_rounding)

                qr = int(fr[i])
                qi = int(fi[i])

                if stage & 1:
                    if precise_rounding:
                        zr >>= 1
                        zi >>= 1
                    qr >>= 1
                    qi >>= 1
                else:
                    if not precise_rounding:
                        zr <<= 1
                        zi <<= 1

                fr[j] = _clamp_s32(qr - zr)
                fi[j] = _clamp_s32(qi - zi)
                fr[i] = _clamp_s32(qr + zr)
                fi[i] = _clamp_s32(qi + zi)

        l = istep
        stage += 1

    centered_cir = np.fft.fftshift((fr + 1.0j * fi).astype(np.complex128) / float(1 << shift) / scale_divisor)
    observed = np.zeros((COMPRESSED_TAP_COUNT,), dtype=np.complex64)
    for i in range(COMPRESSED_TAP_COUNT):
        centered_index = tap_start + i
        observed[i] = np.complex64(centered_cir[centered_index])

    return observed


def _sensor_centered_spectrum_to_ht40_observed_taps_fix32(centered_spectrum: np.ndarray) -> np.ndarray:
    fft_size = COMPRESSED_HT40_FFT_SIZE
    shift = COMPRESSED_HT40_FIX32_SHIFT
    scale_divisor = 8.0
    fr = np.zeros((fft_size,), dtype=np.int64)
    fi = np.zeros((fft_size,), dtype=np.int64)
    natural_input = np.conj(_ifftshift_1d(_sensor_ht40_model_input(np.asarray(centered_spectrum, dtype=np.complex64))))

    for fft_index, coeff in enumerate(natural_input):
        fr[fft_index] = _clamp_s32(int(np.rint(coeff.real * (1 << shift))))
        fi[fft_index] = _clamp_s32(-int(np.rint(coeff.imag * (1 << shift))))

    order = int(np.log2(fft_size))
    for i in range(1, fft_size):
        j = _reverse_bits16(i, order)
        if j <= i:
            continue
        fr[i], fr[j] = fr[j], fr[i]
        fi[i], fi[j] = fi[j], fi[i]

    stage = 0
    l = 1
    while l < fft_size:
        istep = l << 1
        for m in range(l):
            angle = (2.0 * np.pi * m) / istep
            wr = int(np.rint(np.cos(angle) * np.iinfo(np.int32).max))
            wi = int(np.rint(np.sin(angle) * np.iinfo(np.int32).max))

            for i in range(m, fft_size, istep):
                j = i + l
                tmpr = int(fr[j])
                tmpi = int(fi[j])
                zr = _fix32_mpy(wr, tmpr) - _fix32_mpy(wi, tmpi)
                zi = _fix32_mpy(wr, tmpi) + _fix32_mpy(wi, tmpr)
                qr = int(fr[i])
                qi = int(fi[i])

                if stage & 1:
                    qr >>= 1
                    qi >>= 1
                else:
                    zr = _clamp_s32(zr << 1)
                    zi = _clamp_s32(zi << 1)

                fr[j] = _clamp_s32(qr - zr)
                fi[j] = _clamp_s32(qi - zi)
                fr[i] = _clamp_s32(qr + zr)
                fi[i] = _clamp_s32(qi + zi)

        l = istep
        stage += 1

    centered_cir = _fftshift_1d((fr + 1.0j * fi).astype(np.complex128) / float(1 << shift) / scale_divisor)
    observed = np.zeros((COMPRESSED_TAP_COUNT,), dtype=np.complex64)
    for i in range(COMPRESSED_TAP_COUNT):
        centered_index = COMPRESSED_HT40_TAP_START + i
        observed[i] = np.complex64(centered_cir[centered_index])

    return observed


def _sensor_ht40_model_input(centered_spectrum: np.ndarray) -> np.ndarray:
    modeled = np.zeros_like(centered_spectrum, dtype=np.complex64)
    active = _active_slice(COMPRESSED_HT40_FFT_SIZE, HT_COEFFICIENTS_PER_CHANNEL * 2 + HT40_GAP_SUBCARRIERS)
    lower = slice(active.start, active.start + HT_COEFFICIENTS_PER_CHANNEL)
    gap = slice(active.start + HT_COEFFICIENTS_PER_CHANNEL, active.start + HT_COEFFICIENTS_PER_CHANNEL + HT40_GAP_SUBCARRIERS)
    higher = slice(gap.stop, active.stop)
    modeled[lower] = centered_spectrum[lower]
    modeled[gap] = 0.0
    modeled[higher] = centered_spectrum[higher]
    return modeled


def _sensor_centered_spectrum_to_lltf_force_observed_taps_fix32(centered_spectrum: np.ndarray) -> np.ndarray:
    fft_size = COMPRESSED_LLTF_FFT_SIZE
    shift = COMPRESSED_LLTF_FIX32_SHIFT
    scale_divisor = 8.0
    active_start = (fft_size - LEGACY_COEFFICIENTS_PER_CHANNEL) // 2
    fr = np.zeros((fft_size,), dtype=np.int64)
    fi = np.zeros((fft_size,), dtype=np.int64)

    coeff = np.zeros((LEGACY_COEFFICIENTS_PER_CHANNEL,), dtype=np.complex64)
    active_spectrum = np.asarray(centered_spectrum, dtype=np.complex64)
    coeff[:-1:2] = active_spectrum[:-1:2]
    coeff[-1] = 2.0 * coeff[-3] - coeff[-5]
    coeff[1::2] = 0.5 * (coeff[0:-1:2] + coeff[2::2])

    for i in range(LEGACY_COEFFICIENTS_PER_CHANNEL):
        centered_index = active_start + i
        fft_index = (centered_index + fft_size // 2) % fft_size
        fr[fft_index] = int(np.rint(coeff[i].real * (1 << shift)))
        fi[fft_index] = int(np.rint(coeff[i].imag * (1 << shift)))

    order = int(np.log2(fft_size))
    for i in range(1, fft_size):
        j = _reverse_bits16(i, order)
        if j <= i:
            continue
        fr[i], fr[j] = fr[j], fr[i]
        fi[i], fi[j] = fi[j], fi[i]

    stage = 0
    l = 1
    while l < fft_size:
        istep = l << 1
        for m in range(l):
            angle = (2.0 * np.pi * m) / istep
            wr = int(np.rint(np.cos(angle) * np.iinfo(np.int32).max))
            wi = int(np.rint(np.sin(angle) * np.iinfo(np.int32).max))

            for i in range(m, fft_size, istep):
                j = i + l
                tmpr = int(fr[j])
                tmpi = int(fi[j])
                zr = _fix32_mpy(wr, tmpr) - _fix32_mpy(wi, tmpi)
                zi = _fix32_mpy(wr, tmpi) + _fix32_mpy(wi, tmpr)
                qr = int(fr[i])
                qi = int(fi[i])

                if stage & 1:
                    qr >>= 1
                    qi >>= 1
                else:
                    zr <<= 1
                    zi <<= 1

                fr[j] = _clamp_s32(qr - zr)
                fi[j] = _clamp_s32(qi - zi)
                fr[i] = _clamp_s32(qr + zr)
                fi[i] = _clamp_s32(qi + zi)

        l = istep
        stage += 1

    observed = np.zeros((COMPRESSED_TAP_COUNT,), dtype=np.complex64)
    for i in range(COMPRESSED_TAP_COUNT):
        centered_index = COMPRESSED_LLTF_TAP_START + i
        fft_index = (centered_index + fft_size // 2) % fft_size
        observed[i] = np.complex64((float(fr[fft_index]) + 1.0j * float(fi[fft_index])) / float(1 << shift) / scale_divisor)

    return observed


def _sensor_centered_spectrum_to_lltf_observed_taps_fix32(centered_spectrum: np.ndarray) -> np.ndarray:
    fft_size = COMPRESSED_LLTF_FFT_SIZE
    shift = COMPRESSED_LLTF_FIX32_SHIFT
    scale_divisor = 8.0
    active_start = (fft_size - LEGACY_COEFFICIENTS_PER_CHANNEL) // 2
    fr = np.zeros((fft_size,), dtype=np.int64)
    fi = np.zeros((fft_size,), dtype=np.int64)

    coeff = np.asarray(centered_spectrum, dtype=np.complex64).copy()
    coeff[LEGACY_COEFFICIENTS_PER_CHANNEL - 1] = np.complex64(coeff[-1].real + 1.0j * coeff[-3].imag)
    coeff[LEGACY_COEFFICIENTS_PER_CHANNEL // 2] = 0.5 * (coeff[LEGACY_COEFFICIENTS_PER_CHANNEL // 2 - 2] + coeff[LEGACY_COEFFICIENTS_PER_CHANNEL // 2 + 2])
    coeff[1::2] = 0.5 * (coeff[0:-1:2] + coeff[2::2])

    for i in range(LEGACY_COEFFICIENTS_PER_CHANNEL):
        centered_index = active_start + i
        fft_index = (centered_index + fft_size // 2) % fft_size
        fr[fft_index] = _clamp_s32(int(np.rint(coeff[i].real * (1 << shift))))
        fi[fft_index] = _clamp_s32(int(np.rint(coeff[i].imag * (1 << shift))))

    order = int(np.log2(fft_size))
    for i in range(1, fft_size):
        j = _reverse_bits16(i, order)
        if j <= i:
            continue
        fr[i], fr[j] = fr[j], fr[i]
        fi[i], fi[j] = fi[j], fi[i]

    stage = 0
    l = 1
    while l < fft_size:
        istep = l << 1
        for m in range(l):
            angle = (2.0 * np.pi * m) / istep
            wr = int(np.rint(np.cos(angle) * np.iinfo(np.int32).max))
            wi = int(np.rint(np.sin(angle) * np.iinfo(np.int32).max))

            for i in range(m, fft_size, istep):
                j = i + l
                tmpr = int(fr[j])
                tmpi = int(fi[j])
                zr = _fix32_mpy(wr, tmpr) - _fix32_mpy(wi, tmpi)
                zi = _fix32_mpy(wr, tmpi) + _fix32_mpy(wi, tmpr)
                qr = int(fr[i])
                qi = int(fi[i])

                if stage & 1:
                    qr >>= 1
                    qi >>= 1
                else:
                    zr = _clamp_s32(zr << 1)
                    zi = _clamp_s32(zi << 1)

                fr[j] = _clamp_s32(qr - zr)
                fi[j] = _clamp_s32(qi - zi)
                fr[i] = _clamp_s32(qr + zr)
                fi[i] = _clamp_s32(qi + zi)

        l = istep
        stage += 1

    observed = np.zeros((COMPRESSED_TAP_COUNT,), dtype=np.complex64)
    for i in range(COMPRESSED_TAP_COUNT):
        centered_index = COMPRESSED_LLTF_TAP_START + i
        fft_index = (centered_index + fft_size // 2) % fft_size
        observed[i] = np.complex64((float(fr[fft_index]) + 1.0j * float(fi[fft_index])) / float(1 << shift) / scale_divisor)

    return observed


def _build_ht20_sensor_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HT20_FFT_SIZE, HT_COEFFICIENTS_PER_CHANNEL)

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_HT20_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_HT20_FFT_SIZE)
        masked_spectrum = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
        masked_spectrum[active] = centered_spectrum[active]
        masked_spectrum[active][HT_COEFFICIENTS_PER_CHANNEL // 2] = 0.0
        correction[:, col] = _sensor_centered_spectrum_to_ht20_observed_taps(masked_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_ht20_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HT20_FFT_SIZE, HT_COEFFICIENTS_PER_CHANNEL)

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_HT20_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_HT20_FFT_SIZE)
        masked_spectrum = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
        masked_spectrum[active] = centered_spectrum[active]
        masked_spectrum[active][HT_COEFFICIENTS_PER_CHANNEL // 2] = 0.0
        correction[:, col] = _sensor_centered_spectrum_to_ht20_observed_taps_fix32(masked_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_he20_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HE20_FFT_SIZE, HE20_COEFFICIENTS_PER_CHANNEL)
    gap_start = active.start + (HE20_COEFFICIENTS_PER_CHANNEL // 2) - 1

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_HE20_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_HE20_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_HE20_FFT_SIZE)
        masked_spectrum = np.zeros((COMPRESSED_HE20_FFT_SIZE,), dtype=np.complex64)
        masked_spectrum[active] = centered_spectrum[active]
        masked_spectrum[gap_start : gap_start + HT40_GAP_SUBCARRIERS] = 0.0
        correction[:, col] = _sensor_centered_spectrum_to_direct_observed_taps_fix32(
            masked_spectrum,
            COMPRESSED_HE20_FFT_SIZE,
            COMPRESSED_HE20_FIX32_SHIFT,
            COMPRESSED_HE20_TAP_START,
        )

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_lltf_force_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_LLTF_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_LLTF_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_LLTF_FFT_SIZE)
        active_spectrum = centered_spectrum[_active_slice(COMPRESSED_LLTF_FFT_SIZE, LEGACY_COEFFICIENTS_PER_CHANNEL)].copy()
        correction[:, col] = _sensor_centered_spectrum_to_lltf_force_observed_taps_fix32(active_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_lltf_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_LLTF_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_LLTF_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_LLTF_FFT_SIZE)
        active_spectrum = centered_spectrum[_active_slice(COMPRESSED_LLTF_FFT_SIZE, LEGACY_COEFFICIENTS_PER_CHANNEL)].copy()
        correction[:, col] = _sensor_centered_spectrum_to_lltf_observed_taps_fix32(active_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_ht40_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HT40_FFT_SIZE, HT_COEFFICIENTS_PER_CHANNEL * 2 + HT40_GAP_SUBCARRIERS)
    gap_start = active.start + HT_COEFFICIENTS_PER_CHANNEL

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_HT40_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_HT40_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_HT40_FFT_SIZE)
        masked_spectrum = np.zeros((COMPRESSED_HT40_FFT_SIZE,), dtype=np.complex64)
        masked_spectrum[active] = centered_spectrum[active]
        masked_spectrum[gap_start : gap_start + HT40_GAP_SUBCARRIERS] = 0.0
        correction[:, col] = _sensor_centered_spectrum_to_ht40_observed_taps_fix32(masked_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


_COMPRESSED_LLTF_CORRECTION = _build_masked_tap_correction(
    COMPRESSED_LLTF_FFT_SIZE,
    LEGACY_COEFFICIENTS_PER_CHANNEL,
    COMPRESSED_LLTF_TAP_START,
    COMPRESSED_TAP_COUNT,
    [LEGACY_COEFFICIENTS_PER_CHANNEL // 2],
)
_COMPRESSED_LLTF_FORCE_CORRECTION = _build_masked_tap_correction(
    COMPRESSED_LLTF_FFT_SIZE,
    LEGACY_COEFFICIENTS_PER_CHANNEL,
    COMPRESSED_LLTF_TAP_START,
    COMPRESSED_TAP_COUNT,
    [],
)
_COMPRESSED_LLTF_FORCE_FIX32_CORRECTION = _build_lltf_force_fix32_tap_correction()
_COMPRESSED_LLTF_FIX32_CORRECTION = _build_lltf_fix32_tap_correction()
_COMPRESSED_HT20_CORRECTION = _build_ht20_sensor_tap_correction()
_COMPRESSED_HT20_FIX32_CORRECTION = _build_ht20_fix32_tap_correction()
_COMPRESSED_HT40_FIX32_CORRECTION = _build_ht40_fix32_tap_correction()
_COMPRESSED_HE20_FIX32_CORRECTION = _build_he20_fix32_tap_correction()


def _interpolate_ht20_dc(spectrum: np.ndarray) -> np.ndarray:
    spectrum = np.asarray(spectrum, dtype=np.complex64).copy()
    dc_index = HT_COEFFICIENTS_PER_CHANNEL // 2
    spectrum[dc_index] = 0.5 * (spectrum[dc_index - 1] + spectrum[dc_index + 1])
    return spectrum


def _interpolate_he20_gap(spectrum: np.ndarray) -> np.ndarray:
    spectrum = np.asarray(spectrum, dtype=np.complex64).copy()
    center_index = HE20_COEFFICIENTS_PER_CHANNEL // 2
    left = spectrum[center_index - 2]
    right = spectrum[center_index + 2]
    spectrum[center_index - 1 : center_index + 2] = np.asarray((0.25, 0.5, 0.75), dtype=np.float32) * right + np.asarray((0.75, 0.5, 0.25), dtype=np.float32) * left
    return spectrum


def _decode_compressed_tap_window(
    buf,
    fft_size: int,
    tap_start: int,
    tap_count: int,
    active_count: int,
    correction: np.ndarray,
    tap_scale: float,
) -> np.ndarray:
    observed_taps = _decode_wire_complex_i16_scaled(buf, tap_count, tap_scale)
    corrected_taps = np.matmul(correction, observed_taps.astype(np.complex64))
    centered_cir = np.zeros((fft_size,), dtype=np.complex64)
    centered_cir[tap_start : tap_start + tap_count] = corrected_taps
    centered_spectrum = _centered_fft(centered_cir, fft_size)
    return centered_spectrum[_active_slice(fft_size, active_count)].copy()


def decode_compressed_lltf(buf, acquire_force_lltf: bool = False) -> np.ndarray:
    correction = _COMPRESSED_LLTF_FORCE_FIX32_CORRECTION if acquire_force_lltf else _COMPRESSED_LLTF_FIX32_CORRECTION
    spectrum = _decode_compressed_tap_window(
        buf,
        COMPRESSED_LLTF_FFT_SIZE,
        COMPRESSED_LLTF_TAP_START,
        COMPRESSED_TAP_COUNT,
        LEGACY_COEFFICIENTS_PER_CHANNEL,
        correction,
        float((1 << COMPRESSED_LLTF_FIX32_SHIFT) * 8.0),
    )
    if acquire_force_lltf:
        spectrum[-1] = 2.0 * spectrum[-3] - spectrum[-5]
        spectrum[1::2] = 0.5 * (spectrum[0:-1:2] + spectrum[2::2])
    else:
        spectrum[-1] = np.complex64(spectrum[-1].real + 1.0j * spectrum[-3].imag)
        spectrum[LEGACY_COEFFICIENTS_PER_CHANNEL // 2] = 0.5 * (spectrum[LEGACY_COEFFICIENTS_PER_CHANNEL // 2 - 2] + spectrum[LEGACY_COEFFICIENTS_PER_CHANNEL // 2 + 2])
        spectrum[1::2] = 0.5 * (spectrum[0:-1:2] + spectrum[2::2])
    return spectrum


def decode_compressed_ht20(buf) -> np.ndarray:
    spectrum = _decode_compressed_tap_window(
        buf,
        COMPRESSED_HT20_FFT_SIZE,
        COMPRESSED_HT20_TAP_START,
        COMPRESSED_TAP_COUNT,
        HT_COEFFICIENTS_PER_CHANNEL,
        _COMPRESSED_HT20_FIX32_CORRECTION,
        float((1 << COMPRESSED_HT20_FIX32_SHIFT) * 8.0),
    )
    return _interpolate_ht20_dc(spectrum)


def decode_compressed_ht40(buf) -> np.ndarray:
    spectrum = _decode_compressed_tap_window(
        buf,
        COMPRESSED_HT40_FFT_SIZE,
        COMPRESSED_HT40_TAP_START,
        COMPRESSED_TAP_COUNT,
        HT_COEFFICIENTS_PER_CHANNEL * 2 + HT40_GAP_SUBCARRIERS,
        _COMPRESSED_HT40_FIX32_CORRECTION,
        float((1 << COMPRESSED_HT40_FIX32_SHIFT) * 8.0),
    )
    gap_start = HT_COEFFICIENTS_PER_CHANNEL
    spectrum[gap_start : gap_start + HT40_GAP_SUBCARRIERS] = 0.0
    return spectrum


def decode_compressed_he20(buf) -> np.ndarray:
    spectrum = _decode_compressed_tap_window(
        buf,
        COMPRESSED_HE20_FFT_SIZE,
        COMPRESSED_HE20_TAP_START,
        COMPRESSED_TAP_COUNT,
        HE20_COEFFICIENTS_PER_CHANNEL,
        _COMPRESSED_HE20_FIX32_CORRECTION,
        float((1 << COMPRESSED_HE20_FIX32_SHIFT) * 8.0),
    )
    return _interpolate_he20_gap(spectrum)


def _extract_signed15(x: int) -> int:
    x &= 0x7FFF
    return x - 0x8000 if (x & 0x4000) else x


def get_cfo_from_rx_ctrl(rx_ctrl) -> int:
    """
    Compute the CFO value (in Hz) from a `wifi_pkt_rx_ctrl_v3_t` byte buffer.

    This was reverse engineered from librftest.a (bb_common.o) and libphy.a (phy_feature.o)
    """
    ctrl = rx_ctrl if isinstance(rx_ctrl, wifi_pkt_rx_ctrl_v3_t) else wifi_pkt_rx_ctrl_v3_t(rx_ctrl)

    if ctrl.rate_type == 0:
        rate_index = ctrl.rate
    else:
        rate_index = (ctrl.rate_type << 4) + (ctrl.he_siga1 & 0x7F)

    if rate_index < 8:
        return float(_extract_signed15(ctrl.cfo_low_rate) / -48) * 25000 * 5 / 128

    return float((_extract_signed15(ctrl.cfo_high_rate) * -5) / 128) * 25000 * 5 / 128


def deserialize_packet_buffer(revision, pktbuf):
    """
    Deserialize a raw stream payload into the appropriate packet structure based on the type header.
    """
    type_header = int.from_bytes(pktbuf[0:4], byteorder="little")
    if type_header == revision.type_header:
        return revision.serialized_csi_t(pktbuf)
    if type_header == SPI_TYPE_HEADER_RADAR_TX_REPORT:
        return radar_tx_report_tlv_t(pktbuf)

    raise ValueError("Unexpected logical packet type header")


def csistream_uid_to_antid(uid: int) -> int:
    return (uid >> CSISTREAM_UID_SENSOR_SHIFT) & CSISTREAM_UID_SENSOR_MASK


def parse_csistream_jumbo_message(message: bytes) -> bytes:
    if len(message) < 4:
        raise ValueError("CSI stream message too short")

    jumbo = bytes(message)
    if int.from_bytes(jumbo[:4], byteorder="little") != SPI_TYPE_HEADER_JUMBO_FRAME:
        raise ValueError("CSI stream message does not contain a jumbo frame")

    return jumbo


def iter_csistream_fragments(jumbo: bytes):
    if len(jumbo) < 4:
        raise ValueError("Jumbo frame too short")
    if int.from_bytes(jumbo[:4], byteorder="little") != SPI_TYPE_HEADER_JUMBO_FRAME:
        raise ValueError("Invalid jumbo frame type header")

    offset = 4
    header_size = ctypes.sizeof(csistream_fragment_header_t)
    while offset + header_size <= len(jumbo):
        header = csistream_fragment_header_t(jumbo[offset : offset + header_size])
        offset += header_size
        if header.uid == JUMBO_FRAGMENT_TERMINATOR_UID:
            return

        end = offset + header.size
        if end > len(jumbo):
            raise ValueError("Fragment exceeds jumbo frame boundary")

        yield header, jumbo[offset:end]
        offset = end

    raise ValueError("Jumbo frame terminator missing")
