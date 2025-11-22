from enum import IntEnum
import ctypes

from . import constants

# Internal constants
_ESPARGOS_SPI_BUFFER_SIZE = 512

# Other constants
HT_COEFFICIENTS_PER_CHANNEL = 57
"Number of channel coefficients (active subcarriers) per Wi-Fi channel in HT mode (HT-LTF)"

LEGACY_COEFFICIENTS_PER_CHANNEL = 53
"Number of channel coefficients (active subcarriers) per Wi-Fi channel in legacy mode (L-LTF)"

HT40_GAP_SUBCARRIERS = 3
"Gap between primary and secondary channel in HT40 mode, in subcarriers"

#####################################################
#       Enums used by multiple PHY versions         #
#####################################################
class wifi_rx_bb_format_t(IntEnum):
    RX_BB_FORMAT_11B     = 0
    RX_BB_FORMAT_11G     = 1
    RX_BB_FORMAT_11A     = 1  # Same value as 11G
    RX_BB_FORMAT_HT      = 2
    RX_BB_FORMAT_VHT     = 3
    RX_BB_FORMAT_HE_SU   = 4
    RX_BB_FORMAT_HE_MU   = 5
    RX_BB_FORMAT_HE_ERSU = 6
    RX_BB_FORMAT_HE_TB   = 7

#####################################################
# Common C Structures used by multiple PHY versions #
#####################################################
class seq_ctrl_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing the sequence control field of a Wi-Fi packet.

    This structure is used to store the sequence control field of a Wi-Fi packet, which contains the fragment number and the segment number.
    """
    _pack_ = 1
    _fields_ = [
        ("frag", ctypes.c_uint8, 4),
        ("seg", ctypes.c_uint16, 12)
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass

class csistream_pkt_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing a CSI packet as received from the ESPARGOS controller, i.e.,
    sensor number and the raw data buffer that should contain the serialized_csi_v1_t / serialized_csi_v3_t structure if the type_header matches.
    """
    _pack_ = 1
    _fields_ = [
        ("esp_num", ctypes.c_uint32),
        ("buf", ctypes.c_uint8 * _ESPARGOS_SPI_BUFFER_SIZE),
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass

####################################################################
# C Structures for Espressif PHY version 1 (e.g., ESP32, ESP32-S2) #
####################################################################
class wifi_pkt_rx_ctrl_v1_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing the `wifi_pkt_rx_ctrl_t` as provided by the ESP32.
    See the related `esp-idf code <https://github.com/espressif/esp-idf/blob/master/components/esp_wifi/include/local/esp_wifi_types_native.h>`_ for details.
    Variant for Espressif PHY version 1.
    """
    _pack_ = 1

    _fields_ = [
        ("rssi", ctypes.c_uint32, 8),
        ("rate", ctypes.c_uint32, 5),
        ("reserved1", ctypes.c_uint32, 1),
        ("sig_mode", ctypes.c_uint32, 2),
        ("reserved2", ctypes.c_uint32, 16),
        ("mcs", ctypes.c_uint32, 7),
        ("cwb", ctypes.c_uint32, 1),
        ("reserved3", ctypes.c_uint32, 16),
        ("smoothing", ctypes.c_uint32, 1),
        ("not_sounding", ctypes.c_uint32, 1),
        ("reserved4", ctypes.c_uint32, 1),
        ("aggregation", ctypes.c_uint32, 1),
        ("stbc", ctypes.c_uint32, 2),
        ("fec_coding", ctypes.c_uint32, 1),
        ("sgi", ctypes.c_uint32, 1),
        ("reserved5", ctypes.c_uint32, 8),
        ("ampdu_cnt", ctypes.c_uint32, 8),
        ("channel", ctypes.c_uint32, 4),
        ("secondary_channel", ctypes.c_uint32, 4),
        ("rxstart_time_cyc", ctypes.c_uint32, 7),
        ("reserved6", ctypes.c_uint32, 1),
        ("timestamp", ctypes.c_uint32, 32),
        ("reserved7", ctypes.c_uint32, 32),
        ("reserved8", ctypes.c_uint32, 32),
        ("reserved9", ctypes.c_uint32, 20),
        ("rxstart_time_cyc_dec", ctypes.c_uint32, 11),
        ("ant", ctypes.c_uint32, 1),
        ("noise_floor", ctypes.c_uint32, 8),
        ("reserved10", ctypes.c_uint32, 24),
        ("sig_len", ctypes.c_uint32, 12),
        ("reserved11", ctypes.c_uint32, 12),
        ("rx_state", ctypes.c_uint32, 8),
    ]

    def __new__(self, buf=None):
        if buf:
            buf = bytearray(buf)
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass

assert(ctypes.sizeof(wifi_pkt_rx_ctrl_v1_t) == 36)

# 0-5: lltf_guard_below
# 6-58: lltf
# 60-65: lltf_guard_above
# 66-122: htltf primary
# 123-133: htltf_guard_below
# 134-190: htltf secondary
# 191-192: htltf_guard_above
class csi_buf_v1_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing the CSI buffer as produced by the ESP32.

    This structure is used to store the channel coefficients estimated from Wi-Fi packets,
    directly as provided in the :code:`buf` field of :code:`wifi_csi_info_t` by esp-idf, refer to the related `esp-idf documentation <https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/wifi.html#wi-fi-channel-state-information>`_ for details.
    The structure is packed to ensure there is no padding between fields.

    Variant for Espressif PHY version 1.
    """
    _pack_ = 1
    _fields_ = [
        ("lltf_guard_below", ctypes.c_int8 * (6 * 2)), # all zeros
        ("lltf", ctypes.c_int8 * (LEGACY_COEFFICIENTS_PER_CHANNEL * 2)),
        ("lltf_guard_above", ctypes.c_int8 * (7 * 2)), # all zeros
        ("htltf_higher", ctypes.c_int8 * (HT_COEFFICIENTS_PER_CHANNEL * 2)),
        ("htltf_guard_below", ctypes.c_int8 * (11 * 2)), # all zeros
        ("htltf_lower", ctypes.c_int8 * (HT_COEFFICIENTS_PER_CHANNEL * 2)),
        ("htltf_guard_above", ctypes.c_int8 * (1 * 2))
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass

class serialized_csi_v1_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing the CSI buffer and metadata as provided by the ESPARGOS firmware.
    """
    _pack_ = 1
    _fields_ = [
        ("type_header", ctypes.c_uint32),
        ("rx_ctrl", ctypes.c_uint8 * ctypes.sizeof(wifi_pkt_rx_ctrl_v1_t)),
        ("source_mac", ctypes.c_uint8 * 6),
        ("dest_mac", ctypes.c_uint8 * 6),
        ("seq_ctrl", seq_ctrl_t),
        ("timestamp", ctypes.c_uint32),
        ("is_calib", ctypes.c_bool),
        ("first_word_invalid", ctypes.c_bool),
        ("buf", ctypes.c_int8 * (ctypes.sizeof(csi_buf_v1_t))),
        ("global_timestamp_us", ctypes.c_uint64)
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass


########################################################################
# C Structures for Espressif PHY version 3 (e.g., ESP32-C5, ESP32-C61) #
########################################################################
# TODO: This should also contain AGC and FFT gain info
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
        ("_reserved2", ctypes.c_uint32, 2),
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

        ("_reserved5", ctypes.c_uint32, 15),
        ("_reserved6", ctypes.c_uint32, 15),
        ("_reserved7", ctypes.c_uint32, 2),

        ("noise_floor", ctypes.c_uint32, 8),
        ("_reserved8", ctypes.c_uint32, 8),
        ("fft_gain", ctypes.c_uint32, 8),
        ("agc_gain", ctypes.c_uint32, 8),

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

assert(ctypes.sizeof(wifi_pkt_rx_ctrl_v3_t) == 64)

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
        ("htltf_higher", ctypes.c_int8 * (HT_COEFFICIENTS_PER_CHANNEL * 2)),
        ("htltf_gap", ctypes.c_int8 * (3 * 2)), # all zeros
        ("htltf_lower", ctypes.c_int8 * (HT_COEFFICIENTS_PER_CHANNEL * 2)),
        ("reserved", ctypes.c_int8 * (75 * 2))
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
        ("reserved", ctypes.c_int8 * (135 * 2))
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
        ("reserved", ctypes.c_int8 * (139 * 2))
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass

assert(ctypes.sizeof(csi_buf_v3_lltf_t) == ctypes.sizeof(csi_buf_v3_ht20_t) == ctypes.sizeof(csi_buf_v3_ht40_t) == 384)

class serialized_csi_v3_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing the CSI buffer and metadata as provided by the ESPARGOS firmware.

    Variant for Espressif PHY version 3.
    """
    _pack_ = 1
    _fields_ = [
        ("type_header", ctypes.c_uint32),
        ("rx_ctrl", ctypes.c_uint8 * ctypes.sizeof(wifi_pkt_rx_ctrl_v3_t)),
        ("source_mac", ctypes.c_uint8 * 6),
        ("dest_mac", ctypes.c_uint8 * 6),
        ("seq_ctrl", seq_ctrl_t),
        ("timestamp", ctypes.c_uint32),
        ("is_calib", ctypes.c_bool),
        ("first_word_invalid", ctypes.c_bool),
        ("buf", ctypes.c_int8 * (ctypes.sizeof(csi_buf_v3_lltf_t))),
        ("global_timestamp_us", ctypes.c_uint64)
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass

def deserialize_packet_buffer(revision, pktbuf):
    """
    Deserialize a raw buffer into the appropriate serialized CSI structure based on the type header.
    """
    type_header = int.from_bytes(pktbuf[0:4], byteorder="little")
    assert(type_header == revision.type_header)

    # Maps to the correct csi structure (serialized_csi_v1_t or serialized_csi_v3_t) based on the board revision
    return revision.serialized_csi_t(pktbuf)