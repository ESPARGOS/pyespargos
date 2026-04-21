from enum import IntEnum
import ctypes
import numpy as np

from . import constants

# Internal constants
_ESPARGOS_SPI_BUFFER_SIZE_V3 = 384
CSISTREAM_FRAME_PREFIX_SIZE = 4
SPI_TYPE_HEADER_CSI = 0xE4CD0BAC
SPI_TYPE_HEADER_JUMBO_FRAME = 0xDECAFBAD
JUMBO_FRAGMENT_TERMINATOR_UID = 0

# Other constants
HT_COEFFICIENTS_PER_CHANNEL = 57
"Number of channel coefficients (active subcarriers) per Wi-Fi channel in HT mode (HT-LTF)"

LEGACY_COEFFICIENTS_PER_CHANNEL = 53
"Number of channel coefficients (active subcarriers) per Wi-Fi channel in legacy mode (L-LTF)"

HT40_GAP_SUBCARRIERS = 3
"Gap between primary and secondary channel in HT40 mode, in subcarriers"

COMPRESSED_LLTF_FFT_SIZE = 64
COMPRESSED_HT20_FFT_SIZE = 64
COMPRESSED_HT40_FFT_SIZE = 128
COMPRESSED_LLTF_FIX32_SHIFT = 17
COMPRESSED_HT20_FIX32_SHIFT = 21
COMPRESSED_HT40_FIX32_SHIFT = 21
COMPRESSED_LLTF_TAP_START = 27
COMPRESSED_LLTF_TAP_COUNT = 16
COMPRESSED_HT20_TAP_START = 27
COMPRESSED_HT20_TAP_COUNT = 16
COMPRESSED_HT40_TAP_START = 55
COMPRESSED_HT40_TAP_COUNT = 16
HT20_SIMULATION_MODES = (
    "float",
    "float_corrected",
    "sc32",
    "sc32_corrected",
    "fix32",
    "fix32_corrected",
)

SERIALIZED_CSI_INFO_FLAG_RADAR = 1 << 0
SERIALIZED_CSI_INFO_FLAG_LLTF_BIT_MODE = 1 << 1
SERIALIZED_CSI_INFO_FLAG_COMPRESSED_CSI = 1 << 2


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


class csistream_pkt_v3_t(ctypes.LittleEndianStructure):
    """
    A ctypes structure representing a CSI packet as received from the ESPARGOS controller, i.e.,
    sensor number and the raw data buffer that should contain the serialized_csi_v3_t structure if the type_header matches.
    """

    _pack_ = 1
    _fields_ = [
        ("esp_num", ctypes.c_uint32),
        ("buf", ctypes.c_uint8 * _ESPARGOS_SPI_BUFFER_SIZE_V3),
    ]

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


assert ctypes.sizeof(wifi_pkt_rx_ctrl_v3_t) == 64


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
        ("global_timestamp_us", ctypes.c_uint64),
        ("csi_len", ctypes.c_uint16),
        ("acquire_force_lltf", ctypes.c_bool),
        ("acquire_val_scale_cfg", ctypes.c_uint8),
        ("rfswitch_state", ctypes.c_uint32),
        ("info_flags", ctypes.c_uint8),
        ("antid", ctypes.c_uint8),
        ("reserved_meta", ctypes.c_uint8),
        ("crc32", ctypes.c_uint32),
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass

    @property
    def is_radar(self):
        return bool(self.info_flags & SERIALIZED_CSI_INFO_FLAG_RADAR)

    @property
    def acquire_lltf_bit_mode(self):
        return bool(self.info_flags & SERIALIZED_CSI_INFO_FLAG_LLTF_BIT_MODE)

    @property
    def is_compressed(self):
        return bool(self.info_flags & SERIALIZED_CSI_INFO_FLAG_COMPRESSED_CSI)


def _decode_wire_complex_int8(buf, pair_count):
    values = np.asarray(buf[: pair_count * 2], dtype=np.int8).astype(np.float32).view(np.complex64)
    return -1.0j * np.conj(values)


def _decode_wire_complex_float32(buf, pair_count):
    values = np.frombuffer(np.asarray(buf[: pair_count * 8], dtype=np.int8).tobytes(), dtype="<f4")
    return (values[0::2] + 1.0j * values[1::2]).astype(np.complex64)


def unpack_lltf12_values(buf, value_count: int) -> np.ndarray:
    raw = np.asarray(buf[: value_count * 2], dtype=np.uint8)
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

    observed = np.zeros((COMPRESSED_HT20_TAP_COUNT,), dtype=np.complex64)
    for i in range(COMPRESSED_HT20_TAP_COUNT):
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
    fft_size = COMPRESSED_HT20_FFT_SIZE
    shift = COMPRESSED_HT20_FIX32_SHIFT
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
    observed = np.zeros((COMPRESSED_HT20_TAP_COUNT,), dtype=np.complex64)
    for i in range(COMPRESSED_HT20_TAP_COUNT):
        centered_index = COMPRESSED_HT20_TAP_START + i
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
    observed = np.zeros((COMPRESSED_HT40_TAP_COUNT,), dtype=np.complex64)
    for i in range(COMPRESSED_HT40_TAP_COUNT):
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

    observed = np.zeros((COMPRESSED_LLTF_TAP_COUNT,), dtype=np.complex64)
    for i in range(COMPRESSED_LLTF_TAP_COUNT):
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

    observed = np.zeros((COMPRESSED_LLTF_TAP_COUNT,), dtype=np.complex64)
    for i in range(COMPRESSED_LLTF_TAP_COUNT):
        centered_index = COMPRESSED_LLTF_TAP_START + i
        fft_index = (centered_index + fft_size // 2) % fft_size
        observed[i] = np.complex64((float(fr[fft_index]) + 1.0j * float(fi[fft_index])) / float(1 << shift) / scale_divisor)

    return observed


def _build_ht20_sensor_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_HT20_TAP_COUNT, COMPRESSED_HT20_TAP_COUNT), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HT20_FFT_SIZE, HT_COEFFICIENTS_PER_CHANNEL)

    for col in range(COMPRESSED_HT20_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_HT20_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_HT20_FFT_SIZE)
        masked_spectrum = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
        masked_spectrum[active] = centered_spectrum[active]
        masked_spectrum[active][HT_COEFFICIENTS_PER_CHANNEL // 2] = 0.0
        correction[:, col] = _sensor_centered_spectrum_to_ht20_observed_taps(masked_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_ht20_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_HT20_TAP_COUNT, COMPRESSED_HT20_TAP_COUNT), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HT20_FFT_SIZE, HT_COEFFICIENTS_PER_CHANNEL)

    for col in range(COMPRESSED_HT20_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_HT20_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_HT20_FFT_SIZE)
        masked_spectrum = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
        masked_spectrum[active] = centered_spectrum[active]
        masked_spectrum[active][HT_COEFFICIENTS_PER_CHANNEL // 2] = 0.0
        correction[:, col] = _sensor_centered_spectrum_to_ht20_observed_taps_fix32(masked_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_lltf_force_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_LLTF_TAP_COUNT, COMPRESSED_LLTF_TAP_COUNT), dtype=np.complex64)

    for col in range(COMPRESSED_LLTF_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_LLTF_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_LLTF_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_LLTF_FFT_SIZE)
        active_spectrum = centered_spectrum[_active_slice(COMPRESSED_LLTF_FFT_SIZE, LEGACY_COEFFICIENTS_PER_CHANNEL)].copy()
        correction[:, col] = _sensor_centered_spectrum_to_lltf_force_observed_taps_fix32(active_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_lltf_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_LLTF_TAP_COUNT, COMPRESSED_LLTF_TAP_COUNT), dtype=np.complex64)

    for col in range(COMPRESSED_LLTF_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_LLTF_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_LLTF_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_LLTF_FFT_SIZE)
        active_spectrum = centered_spectrum[_active_slice(COMPRESSED_LLTF_FFT_SIZE, LEGACY_COEFFICIENTS_PER_CHANNEL)].copy()
        correction[:, col] = _sensor_centered_spectrum_to_lltf_observed_taps_fix32(active_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_ht40_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_HT40_TAP_COUNT, COMPRESSED_HT40_TAP_COUNT), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HT40_FFT_SIZE, HT_COEFFICIENTS_PER_CHANNEL * 2 + HT40_GAP_SUBCARRIERS)
    gap_start = active.start + HT_COEFFICIENTS_PER_CHANNEL

    for col in range(COMPRESSED_HT40_TAP_COUNT):
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
    COMPRESSED_LLTF_TAP_COUNT,
    [LEGACY_COEFFICIENTS_PER_CHANNEL // 2],
)
_COMPRESSED_LLTF_FORCE_CORRECTION = _build_masked_tap_correction(
    COMPRESSED_LLTF_FFT_SIZE,
    LEGACY_COEFFICIENTS_PER_CHANNEL,
    COMPRESSED_LLTF_TAP_START,
    COMPRESSED_LLTF_TAP_COUNT,
    [],
)
_COMPRESSED_LLTF_FORCE_FIX32_CORRECTION = _build_lltf_force_fix32_tap_correction()
_COMPRESSED_LLTF_FIX32_CORRECTION = _build_lltf_fix32_tap_correction()
_COMPRESSED_HT20_FLOAT_CORRECTION = _build_masked_tap_correction(
    COMPRESSED_HT20_FFT_SIZE,
    HT_COEFFICIENTS_PER_CHANNEL,
    COMPRESSED_HT20_TAP_START,
    COMPRESSED_HT20_TAP_COUNT,
    [HT_COEFFICIENTS_PER_CHANNEL // 2],
)
_COMPRESSED_HT20_CORRECTION = _build_ht20_sensor_tap_correction()
_COMPRESSED_HT20_FIX32_CORRECTION = _build_ht20_fix32_tap_correction()
_COMPRESSED_HT40_FIX32_CORRECTION = _build_ht40_fix32_tap_correction()


def _build_ht20_masked_centered_spectrum(spectrum: np.ndarray) -> np.ndarray:
    centered = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HT20_FFT_SIZE, HT_COEFFICIENTS_PER_CHANNEL)
    centered[active] = np.asarray(spectrum, dtype=np.complex64)
    centered[active][HT_COEFFICIENTS_PER_CHANNEL // 2] = 0.0
    return centered


def _ht20_float_observed_taps(centered_spectrum: np.ndarray) -> np.ndarray:
    centered_cir = _centered_ifft(centered_spectrum)
    return centered_cir[COMPRESSED_HT20_TAP_START : COMPRESSED_HT20_TAP_START + COMPRESSED_HT20_TAP_COUNT].astype(np.complex64)


def _recover_ht20_spectrum_from_taps(observed_taps: np.ndarray, correction: np.ndarray | None) -> np.ndarray:
    taps = np.asarray(observed_taps, dtype=np.complex64)
    if correction is not None:
        taps = np.matmul(correction, taps.astype(np.complex64))

    centered_cir = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
    centered_cir[COMPRESSED_HT20_TAP_START : COMPRESSED_HT20_TAP_START + COMPRESSED_HT20_TAP_COUNT] = taps
    centered_spectrum = _centered_fft(centered_cir, COMPRESSED_HT20_FFT_SIZE)
    spectrum = centered_spectrum[_active_slice(COMPRESSED_HT20_FFT_SIZE, HT_COEFFICIENTS_PER_CHANNEL)].copy()
    spectrum[HT_COEFFICIENTS_PER_CHANNEL // 2] = 0.0
    return spectrum


def _interpolate_ht20_dc(spectrum: np.ndarray) -> np.ndarray:
    spectrum = np.asarray(spectrum, dtype=np.complex64).copy()
    dc_index = HT_COEFFICIENTS_PER_CHANNEL // 2
    spectrum[dc_index] = 0.5 * (spectrum[dc_index - 1] + spectrum[dc_index + 1])
    return spectrum


def simulate_ht20_compression(spectrum: np.ndarray, mode: str) -> np.ndarray:
    centered = _build_ht20_masked_centered_spectrum(spectrum)

    if mode == "float":
        taps = _ht20_float_observed_taps(centered)
        return _interpolate_ht20_dc(_recover_ht20_spectrum_from_taps(taps, None))
    if mode == "float_corrected":
        taps = _ht20_float_observed_taps(centered)
        return _interpolate_ht20_dc(_recover_ht20_spectrum_from_taps(taps, _COMPRESSED_HT20_FLOAT_CORRECTION))
    if mode == "sc32":
        taps = _sensor_centered_spectrum_to_ht20_observed_taps(centered)
        return _interpolate_ht20_dc(_recover_ht20_spectrum_from_taps(taps, None))
    if mode == "sc32_corrected":
        taps = _sensor_centered_spectrum_to_ht20_observed_taps(centered)
        return _interpolate_ht20_dc(_recover_ht20_spectrum_from_taps(taps, _COMPRESSED_HT20_CORRECTION))
    if mode == "fix32":
        taps = _sensor_centered_spectrum_to_ht20_observed_taps_fix32(centered)
        return _interpolate_ht20_dc(_recover_ht20_spectrum_from_taps(taps, None))
    if mode == "fix32_corrected":
        taps = _sensor_centered_spectrum_to_ht20_observed_taps_fix32(centered)
        return _interpolate_ht20_dc(_recover_ht20_spectrum_from_taps(taps, _COMPRESSED_HT20_FIX32_CORRECTION))

    raise ValueError(f"Unknown HT20 simulation mode: {mode}")


def _decode_compressed_tap_window(
    buf,
    fft_size: int,
    tap_start: int,
    tap_count: int,
    active_count: int,
    correction: np.ndarray,
) -> np.ndarray:
    observed_taps = _decode_wire_complex_float32(buf, tap_count)
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
        COMPRESSED_LLTF_TAP_COUNT,
        LEGACY_COEFFICIENTS_PER_CHANNEL,
        correction,
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
        COMPRESSED_HT20_TAP_COUNT,
        HT_COEFFICIENTS_PER_CHANNEL,
        _COMPRESSED_HT20_FIX32_CORRECTION,
    )
    return _interpolate_ht20_dc(spectrum)


def decode_compressed_ht40(buf) -> np.ndarray:
    spectrum = _decode_compressed_tap_window(
        buf,
        COMPRESSED_HT40_FFT_SIZE,
        COMPRESSED_HT40_TAP_START,
        COMPRESSED_HT40_TAP_COUNT,
        HT_COEFFICIENTS_PER_CHANNEL * 2 + HT40_GAP_SUBCARRIERS,
        _COMPRESSED_HT40_FIX32_CORRECTION,
    )
    gap_start = HT_COEFFICIENTS_PER_CHANNEL
    spectrum[gap_start : gap_start + HT40_GAP_SUBCARRIERS] = 0.0
    return spectrum


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
    Deserialize a raw buffer into the appropriate serialized CSI structure based on the type header.
    """
    type_header = int.from_bytes(pktbuf[0:4], byteorder="little")
    assert type_header == revision.type_header

    return revision.serialized_csi_t(pktbuf)


def parse_csistream_jumbo_message(message: bytes) -> tuple[int, bytes]:
    if len(message) < CSISTREAM_FRAME_PREFIX_SIZE + 4:
        raise ValueError("CSI stream message too short")

    esp_num = int.from_bytes(message[:CSISTREAM_FRAME_PREFIX_SIZE], byteorder="little")
    jumbo = message[CSISTREAM_FRAME_PREFIX_SIZE:]
    if int.from_bytes(jumbo[:4], byteorder="little") != SPI_TYPE_HEADER_JUMBO_FRAME:
        raise ValueError("CSI stream message does not contain a jumbo frame")

    return esp_num, jumbo


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
