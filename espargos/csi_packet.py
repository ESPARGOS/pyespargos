"""Parse CSI sensor messages and expose CSI-specific metadata.

Sensor transport framing and fragment reassembly live in :mod:`espargos.sensor`.
Compact tap reconstruction lives in :mod:`espargos.csi_compression`, while
radar transmit reports have their own parser in :mod:`espargos.radar_packet`.
"""

from enum import IntEnum
import ctypes
import binascii
import numpy as np

from . import constants
from .sensor import RFSwitchState
from .wifi import SequenceControl

# Other constants
HT_COEFFICIENTS_PER_CHANNEL = 57
"Number of channel coefficients (active subcarriers) per Wi-Fi channel in HT mode (HT-LTF)"

LEGACY_COEFFICIENTS_PER_CHANNEL = 53
"Number of channel coefficients (active subcarriers) per Wi-Fi channel in legacy mode (L-LTF)"

HE20_COEFFICIENTS_PER_CHANNEL = 245
"Number of channel coefficients (active plus invalid subcarriers) for a 20 MHz HE-LTF"

HT40_GAP_SUBCARRIERS = 3
"Gap between primary and secondary channel in HT40 mode, in subcarriers"

SERIALIZED_CSI_TLV_TYPE_FRAME_META = 2
SERIALIZED_CSI_TLV_TYPE_TIMING_META = 3
SERIALIZED_CSI_TLV_TYPE_ACQUIRE_META = 4
SERIALIZED_CSI_TLV_TYPE_RX_CTRL_RAW = 5
SERIALIZED_CSI_TLV_TYPE_CSI_RAW = 6
SERIALIZED_CSI_TLV_TYPE_CSI_COMPRESSED = 7
SERIALIZED_CSI_TLV_TYPE_RX_CTRL_COMPRESSED = 8
SERIALIZED_CSI_TLV_TYPE_GAIN_TABLE_ENTRY = 9
SERIALIZED_CSI_TLV_TYPE_CRC32 = 255

SERIALIZED_CSI_TLV_FRAME_FLAG_IS_CALIB = 1 << 0
SERIALIZED_CSI_TLV_FRAME_FLAG_IS_RADAR = 1 << 1
SERIALIZED_CSI_TLV_FRAME_FLAG_FIRST_WORD_INVALID = 1 << 2

SERIALIZED_CSI_TLV_ACQUIRE_FLAG_FORCE_LLTF = 1 << 0
SERIALIZED_CSI_TLV_ACQUIRE_FLAG_LLTF_8BIT_MODE = 1 << 1


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


class wifi_sig_mode_t(IntEnum):
    SIG_MODE_LEGACY = 0
    SIG_MODE_HT = 1
    SIG_MODE_HE = 2
    SIG_MODE_VHT = 3


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
    _layout_ = "ms"

    _fields_ = [
        ("rssi", ctypes.c_uint32, 8),
        ("rate", ctypes.c_uint32, 5),
        ("lsig_reserved", ctypes.c_uint32, 1),
        ("sig_mode", ctypes.c_uint32, 2),
        ("lsig_len", ctypes.c_uint32, 12),
        ("rxmatch0", ctypes.c_uint32, 1),
        ("rxmatch1", ctypes.c_uint32, 1),
        ("rxmatch2", ctypes.c_uint32, 1),
        ("rxmatch3", ctypes.c_uint32, 1),
        ("he_siga1", ctypes.c_uint32, 32),  # HE-SIGA1, HT-SIG, or VHT-SIG depending on cur_bb_format
        ("rxend_state", ctypes.c_uint32, 8),
        ("he_siga2", ctypes.c_uint32, 16),
        ("rxstart_time_cyc", ctypes.c_uint32, 7),
        ("is_group", ctypes.c_uint32, 1),
        ("timestamp", ctypes.c_uint32, 32),
        ("cfo_low_rate", ctypes.c_uint32, 15),
        ("cfo_high_rate", ctypes.c_uint32, 15),
        ("_reserved7", ctypes.c_uint32, 2),
        ("noise_floor", ctypes.c_uint32, 8),
        ("data_rssi", ctypes.c_uint32, 8),  # signed data-field RSSI estimate, exposed as raw uint8
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


def gain_byte_to_signed(value: int) -> int:
    """
    Interpret an 8-bit gain value reported in ``rx_ctrl`` as signed.

    The hardware stores gain fields as bytes. RX gain normally lives in the
    positive gain-table range, while FFT gain can be negative and is therefore
    reported in two's-complement form.
    """
    value = int(value)
    value &= 0xFF
    return value - 0x100 if value & 0x80 else value


class CSIPacket:
    def __init__(self, buf=None):
        # Imported lazily to keep packet framing independent while avoiding a
        # module cycle: compression reconstruction uses the structures and
        # packet-layout constants defined in this module.
        from . import csi_compression

        raw = bytes(buf if buf is not None else b"")
        if len(raw) < 4:
            raise ValueError("CSI TLV packet too short")

        self._raw = raw
        self.type_header = int.from_bytes(raw[0:4], byteorder="little")
        self.source_mac = bytes(6)
        self.dest_mac = bytes(6)
        self.seq_ctrl = SequenceControl(b"\x00\x00")
        self.frame_flags = 0
        self.timestamp = 0
        self.global_timestamp_us = 0
        self.acquire_flags = 0
        self.acquire_val_scale_cfg = 0
        self.rfswitch_state = RFSwitchState.SENSOR_RFSWITCH_UNKNOWN
        self.gain_table_entry_raw = bytes(12)
        self.gain_table_entry_valid = False
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
                self.seq_ctrl = SequenceControl(value[12:14])
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
                if tlv_len < csi_compression.COMPRESSED_RX_CTRL_MIN_SIZE:
                    raise ValueError("Invalid compressed RX CTRL TLV")
                self.rx_ctrl = csi_compression.build_rx_ctrl_v3_from_compressed(bytes(value[: ctypes.sizeof(csi_compression.CompressedRxControl)]))
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
                logical_csi_len = min(1 + csi_compression.COMPRESSED_TAP_COUNT * 4, self._raw_csi_padded_len)
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
    def acquire_lltf_8bit_mode(self):
        return bool(self.acquire_flags & SERIALIZED_CSI_TLV_ACQUIRE_FLAG_LLTF_8BIT_MODE)

    @property
    def acquire_lltf_bit_mode(self):
        return self.acquire_lltf_8bit_mode

    @property
    def is_compressed(self):
        return self._is_compressed


def unpack_lltf8_values(buf, pair_count):
    values = np.frombuffer(buf[: pair_count * 2], dtype=np.int8).astype(np.float32).view(np.complex64)
    return -1.0j * np.conj(values)


def unpack_lltf12_values(buf, value_count: int) -> np.ndarray:
    raw = np.frombuffer(buf[: value_count * 2], dtype=np.uint8)
    words = (raw[0::2].astype(np.uint16) | (raw[1::2].astype(np.uint16) << 8)).astype(np.uint16)
    return (((words.astype(np.int16) << 4) >> 4)).astype(np.int16)


def _extract_signed15(x: int) -> int:
    x &= 0x7FFF
    return x - 0x8000 if (x & 0x4000) else x


def get_cfo_from_rx_ctrl(rx_ctrl) -> int:
    """
    Compute the CFO value (in Hz) from a `wifi_pkt_rx_ctrl_v3_t` byte buffer.

    This was reverse engineered from librftest.a (bb_common.o) and libphy.a (phy_feature.o).

    Espressif's RX metadata exposes two CFO fields. The low-rate field is used
    when the derived ``rate_index`` is below 8 (802.11b); otherwise the high-rate
    field is used (802.11g/n/ax).
    """
    ctrl = rx_ctrl if isinstance(rx_ctrl, wifi_pkt_rx_ctrl_v3_t) else wifi_pkt_rx_ctrl_v3_t(rx_ctrl)

    if ctrl.sig_mode == wifi_sig_mode_t.SIG_MODE_LEGACY:
        rate_index = ctrl.rate
    else:
        rate_index = (ctrl.sig_mode << 4) + (ctrl.he_siga1 & 0x7F)

    if rate_index < 8:
        return float(_extract_signed15(ctrl.cfo_low_rate) / -48) * 25000 * 5 / 128

    return float((_extract_signed15(ctrl.cfo_high_rate) * -5) / 128) * 25000 * 5 / 128
