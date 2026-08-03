"""Decode the firmware's bandwidth-saving CSI representation.

A frequency-domain channel estimate contains up to hundreds of complex
coefficients per sensor and received frame.  When CSI compression is enabled,
the sensor firmware transforms that estimate into a channel impulse response
and transmits a window of 16 complex time-domain taps with a shared scale
factor.  This exploits the fact that typical wireless channels concentrate
most of their energy in a short delay interval.  Receive metadata is compacted
as well, further reducing the bandwidth required by a large array.

The decoders below undo the fixed-point scaling, apply the required
hardware-specific corrections, and transform the retained taps back into
L-LTF, HT20, HT40, or HE20 frequency-domain CSI.  The representation is lossy:
time-domain energy outside the transmitted tap window cannot be recovered.
"""

import ctypes

import numpy as np

from . import csi_packet
from . import csi_processing

__all__ = [
    "CompressedRxControl",
    "decode_compressed_he20",
    "decode_compressed_ht20",
    "decode_compressed_ht40",
    "decode_compressed_lltf",
]

COMPRESSED_LLTF_FFT_SIZE = 64
COMPRESSED_HT20_FFT_SIZE = 64
COMPRESSED_HT40_FFT_SIZE = 128
COMPRESSED_HE20_FFT_SIZE = 256
COMPRESSED_LLTF_FIX32_SHIFT = 17
COMPRESSED_LLTF_8BIT_MODE_FIX32_SHIFT = 21
COMPRESSED_HT20_FIX32_SHIFT = 21
COMPRESSED_HT40_FIX32_SHIFT = 21
COMPRESSED_HE20_FIX32_SHIFT = 21
COMPRESSED_TAP_COUNT = 16
COMPRESSED_LLTF_TAP_START = 27
COMPRESSED_HT20_TAP_START = 27
COMPRESSED_HT40_TAP_START = 55
COMPRESSED_HE20_TAP_START = 120

RX_CONTROL_FLAG_IS_HT40 = 1 << 0
RX_CONTROL_FLAG_CHANNEL_ESTIMATE_INFO_VALID = 1 << 1


class CompressedRxControl(ctypes.LittleEndianStructure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = [
        ("rssi", ctypes.c_uint8),
        ("noise_floor", ctypes.c_uint8),
        ("channel", ctypes.c_uint8),
        ("secondary_channel", ctypes.c_int8),
        ("cur_bb_format", ctypes.c_uint8),
        ("rate", ctypes.c_uint8),
        ("sig_mode", ctypes.c_uint8),
        ("rxstart_time_cyc", ctypes.c_uint8),
        ("rx_channel_estimate_len", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("timestamp", ctypes.c_uint32),
        ("cfo_low_rate", ctypes.c_uint16),
        ("cfo_high_rate", ctypes.c_uint16),
        ("he_sig1_mcs", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8),
        ("fft_gain", ctypes.c_uint8),
        ("rx_gain", ctypes.c_uint8),
    ]

    def __new__(self, buf=None):
        return self.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass


assert ctypes.sizeof(CompressedRxControl) == 24
COMPRESSED_RX_CTRL_MIN_SIZE = 22


def _build_rx_ctrl_v3_from_compressed(compact_raw: bytes) -> bytes:
    compact_buf = bytes(compact_raw)
    if len(compact_buf) < ctypes.sizeof(CompressedRxControl):
        compact_buf += bytes(ctypes.sizeof(CompressedRxControl) - len(compact_buf))
    compact = CompressedRxControl(compact_buf)
    ctrl = csi_packet.WiFiPacketRxControlV3(bytes(ctypes.sizeof(csi_packet.WiFiPacketRxControlV3)))
    ctrl.rssi = int(compact.rssi)
    ctrl.rate = int(compact.rate)
    ctrl.sig_mode = int(compact.sig_mode)
    ctrl.he_siga1 = int(compact.he_sig1_mcs)
    if compact.flags & RX_CONTROL_FLAG_IS_HT40:
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
    ctrl.fft_gain = int(compact.fft_gain)
    ctrl.rx_gain = int(compact.rx_gain)
    ctrl.rx_channel_estimate_len = int(compact.rx_channel_estimate_len)
    ctrl.rx_channel_estimate_info_vld = 1 if (compact.flags & RX_CONTROL_FLAG_CHANNEL_ESTIMATE_INFO_VALID) else 0
    return ctypes.string_at(ctypes.byref(ctrl), ctypes.sizeof(ctrl))


def _decode_wire_complex_i16_scaled(buf, pair_count, tap_scale: float):
    right_shift = int(np.frombuffer(buf[:1], dtype=np.uint8)[0])
    values = np.frombuffer(buf[1 : 1 + pair_count * 4], dtype="<i2").astype(np.float32)
    values *= float(1 << right_shift) / tap_scale
    return (values[0::2] + 1.0j * values[1::2]).astype(np.complex64)


def _ifftshift_1d(values: np.ndarray) -> np.ndarray:
    half = values.shape[-1] // 2
    return np.concatenate((values[..., half:], values[..., :half]), axis=-1)


def _fftshift_1d(values: np.ndarray) -> np.ndarray:
    half = values.shape[-1] // 2
    return np.concatenate((values[..., -half:], values[..., :-half]), axis=-1)


def _centered_fft(values: np.ndarray, fft_size: int) -> np.ndarray:
    return _fftshift_1d(np.fft.fft(_ifftshift_1d(values), n=fft_size)).astype(np.complex64)


def _active_slice(fft_size: int, active_count: int) -> slice:
    start = (fft_size - active_count) // 2
    return slice(start, start + active_count)


def _clamp_s32(value: int) -> int:
    return max(min(int(value), (1 << 31) - 1), -(1 << 31))


def _reverse_bits16(x: int, order: int) -> int:
    b = x & 0xFFFF
    b = (((b & 0xFF00) >> 8) | ((b & 0x00FF) << 8)) & 0xFFFF
    b = (((b & 0xF0F0) >> 4) | ((b & 0x0F0F) << 4)) & 0xFFFF
    b = (((b & 0xCCCC) >> 2) | ((b & 0x3333) << 2)) & 0xFFFF
    b = (((b & 0xAAAA) >> 1) | ((b & 0x5555) << 1)) & 0xFFFF
    return b >> (16 - order)


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
    active = _active_slice(COMPRESSED_HT40_FFT_SIZE, csi_packet.HT_COEFFICIENTS_PER_CHANNEL * 2 + csi_packet.HT40_GAP_SUBCARRIERS)
    lower = slice(active.start, active.start + csi_packet.HT_COEFFICIENTS_PER_CHANNEL)
    gap = slice(active.start + csi_packet.HT_COEFFICIENTS_PER_CHANNEL, active.start + csi_packet.HT_COEFFICIENTS_PER_CHANNEL + csi_packet.HT40_GAP_SUBCARRIERS)
    higher = slice(gap.stop, active.stop)
    modeled[lower] = centered_spectrum[lower]
    modeled[gap] = 0.0
    modeled[higher] = centered_spectrum[higher]
    return modeled


def _sensor_centered_spectrum_to_lltf_force_observed_taps_fix32(centered_spectrum: np.ndarray) -> np.ndarray:
    fft_size = COMPRESSED_LLTF_FFT_SIZE
    shift = COMPRESSED_LLTF_FIX32_SHIFT
    scale_divisor = 8.0
    active_start = (fft_size - csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL) // 2
    fr = np.zeros((fft_size,), dtype=np.int64)
    fi = np.zeros((fft_size,), dtype=np.int64)

    coeff = np.zeros((csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL,), dtype=np.complex64)
    active_spectrum = np.asarray(centered_spectrum, dtype=np.complex64)
    coeff[:-1:2] = active_spectrum[:-1:2]
    coeff[-1] = 2.0 * coeff[-3] - coeff[-5]
    coeff[1::2] = 0.5 * (coeff[0:-1:2] + coeff[2::2])

    for i in range(csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL):
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
    active_start = (fft_size - csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL) // 2
    fr = np.zeros((fft_size,), dtype=np.int64)
    fi = np.zeros((fft_size,), dtype=np.int64)

    coeff = np.asarray(centered_spectrum, dtype=np.complex64).copy()
    coeff[csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL - 1] = np.complex64(coeff[-1].real + 1.0j * coeff[-3].imag)
    coeff[csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL // 2] = 0.5 * (coeff[csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL // 2 - 2] + coeff[csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL // 2 + 2])
    coeff[1::2] = 0.5 * (coeff[0:-1:2] + coeff[2::2])

    for i in range(csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL):
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


def _sensor_centered_spectrum_to_lltf_8bit_mode_observed_taps_fix32(centered_spectrum: np.ndarray) -> np.ndarray:
    active = _active_slice(COMPRESSED_LLTF_FFT_SIZE, csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL)
    modeled = np.zeros((COMPRESSED_LLTF_FFT_SIZE,), dtype=np.complex64)
    modeled[active] = np.asarray(centered_spectrum, dtype=np.complex64)
    modeled[active.start + csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL // 2] = 0.0
    return _sensor_centered_spectrum_to_direct_observed_taps_fix32(
        modeled,
        COMPRESSED_LLTF_FFT_SIZE,
        COMPRESSED_LLTF_8BIT_MODE_FIX32_SHIFT,
        COMPRESSED_LLTF_TAP_START,
    )


def _build_ht20_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HT20_FFT_SIZE, csi_packet.HT_COEFFICIENTS_PER_CHANNEL)

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_HT20_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_HT20_FFT_SIZE)
        masked_spectrum = np.zeros((COMPRESSED_HT20_FFT_SIZE,), dtype=np.complex64)
        masked_spectrum[active] = centered_spectrum[active]
        masked_spectrum[active][csi_packet.HT_COEFFICIENTS_PER_CHANNEL // 2] = 0.0
        correction[:, col] = _sensor_centered_spectrum_to_ht20_observed_taps_fix32(masked_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_he20_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HE20_FFT_SIZE, csi_packet.HE20_COEFFICIENTS_PER_CHANNEL)
    gap_start = active.start + (csi_packet.HE20_COEFFICIENTS_PER_CHANNEL // 2) - 1

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_HE20_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_HE20_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_HE20_FFT_SIZE)
        masked_spectrum = np.zeros((COMPRESSED_HE20_FFT_SIZE,), dtype=np.complex64)
        masked_spectrum[active] = centered_spectrum[active]
        masked_spectrum[gap_start : gap_start + csi_packet.HT40_GAP_SUBCARRIERS] = 0.0
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
        active_spectrum = centered_spectrum[_active_slice(COMPRESSED_LLTF_FFT_SIZE, csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL)].copy()
        correction[:, col] = _sensor_centered_spectrum_to_lltf_force_observed_taps_fix32(active_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_lltf_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_LLTF_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_LLTF_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_LLTF_FFT_SIZE)
        active_spectrum = centered_spectrum[_active_slice(COMPRESSED_LLTF_FFT_SIZE, csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL)].copy()
        correction[:, col] = _sensor_centered_spectrum_to_lltf_observed_taps_fix32(active_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_lltf_8bit_mode_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_LLTF_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_LLTF_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_LLTF_FFT_SIZE)
        active_spectrum = centered_spectrum[_active_slice(COMPRESSED_LLTF_FFT_SIZE, csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL)].copy()
        correction[:, col] = _sensor_centered_spectrum_to_lltf_8bit_mode_observed_taps_fix32(active_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


def _build_ht40_fix32_tap_correction() -> np.ndarray:
    correction = np.zeros((COMPRESSED_TAP_COUNT, COMPRESSED_TAP_COUNT), dtype=np.complex64)
    active = _active_slice(COMPRESSED_HT40_FFT_SIZE, csi_packet.HT_COEFFICIENTS_PER_CHANNEL * 2 + csi_packet.HT40_GAP_SUBCARRIERS)
    gap_start = active.start + csi_packet.HT_COEFFICIENTS_PER_CHANNEL

    for col in range(COMPRESSED_TAP_COUNT):
        centered_cir = np.zeros((COMPRESSED_HT40_FFT_SIZE,), dtype=np.complex64)
        centered_cir[COMPRESSED_HT40_TAP_START + col] = 1.0
        centered_spectrum = _centered_fft(centered_cir, COMPRESSED_HT40_FFT_SIZE)
        masked_spectrum = np.zeros((COMPRESSED_HT40_FFT_SIZE,), dtype=np.complex64)
        masked_spectrum[active] = centered_spectrum[active]
        masked_spectrum[gap_start : gap_start + csi_packet.HT40_GAP_SUBCARRIERS] = 0.0
        correction[:, col] = _sensor_centered_spectrum_to_ht40_observed_taps_fix32(masked_spectrum)

    return np.linalg.pinv(correction).astype(np.complex64)


_COMPRESSED_LLTF_FORCE_FIX32_CORRECTION = _build_lltf_force_fix32_tap_correction()
_COMPRESSED_LLTF_FIX32_CORRECTION = _build_lltf_fix32_tap_correction()
_COMPRESSED_LLTF_8BIT_MODE_FIX32_CORRECTION = _build_lltf_8bit_mode_fix32_tap_correction()
_COMPRESSED_HT20_FIX32_CORRECTION = _build_ht20_fix32_tap_correction()
_COMPRESSED_HT40_FIX32_CORRECTION = _build_ht40_fix32_tap_correction()
_COMPRESSED_HE20_FIX32_CORRECTION = _build_he20_fix32_tap_correction()


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


def decode_compressed_lltf(buf, acquire_force_lltf: bool = False, lltf_8bit_mode: bool = False, **kwargs) -> np.ndarray:
    if "lltf_bit_mode" in kwargs:
        lltf_8bit_mode = bool(kwargs.pop("lltf_bit_mode"))
    if kwargs:
        raise TypeError(f"unexpected keyword argument: {next(iter(kwargs))}")
    if lltf_8bit_mode:
        correction = _COMPRESSED_LLTF_8BIT_MODE_FIX32_CORRECTION
        shift = COMPRESSED_LLTF_8BIT_MODE_FIX32_SHIFT
    else:
        correction = _COMPRESSED_LLTF_FORCE_FIX32_CORRECTION if acquire_force_lltf else _COMPRESSED_LLTF_FIX32_CORRECTION
        shift = COMPRESSED_LLTF_FIX32_SHIFT
    spectrum = _decode_compressed_tap_window(
        buf,
        COMPRESSED_LLTF_FFT_SIZE,
        COMPRESSED_LLTF_TAP_START,
        COMPRESSED_TAP_COUNT,
        csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL,
        correction,
        float((1 << shift) * 8.0),
    )
    if lltf_8bit_mode:
        csi_processing.interpolate_lltf_gap(spectrum)
        return spectrum
    if acquire_force_lltf:
        spectrum[-1] = 2.0 * spectrum[-3] - spectrum[-5]
        spectrum[1::2] = 0.5 * (spectrum[0:-1:2] + spectrum[2::2])
    else:
        spectrum[-1] = np.complex64(spectrum[-1].real + 1.0j * spectrum[-3].imag)
        spectrum[csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL // 2] = 0.5 * (spectrum[csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL // 2 - 2] + spectrum[csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL // 2 + 2])
        spectrum[1::2] = 0.5 * (spectrum[0:-1:2] + spectrum[2::2])
    return spectrum


def decode_compressed_ht20(buf) -> np.ndarray:
    spectrum = _decode_compressed_tap_window(
        buf,
        COMPRESSED_HT20_FFT_SIZE,
        COMPRESSED_HT20_TAP_START,
        COMPRESSED_TAP_COUNT,
        csi_packet.HT_COEFFICIENTS_PER_CHANNEL,
        _COMPRESSED_HT20_FIX32_CORRECTION,
        float((1 << COMPRESSED_HT20_FIX32_SHIFT) * 8.0),
    )
    csi_processing.interpolate_ht20ltf_gap(spectrum)
    return spectrum


def decode_compressed_ht40(buf) -> np.ndarray:
    spectrum = _decode_compressed_tap_window(
        buf,
        COMPRESSED_HT40_FFT_SIZE,
        COMPRESSED_HT40_TAP_START,
        COMPRESSED_TAP_COUNT,
        csi_packet.HT_COEFFICIENTS_PER_CHANNEL * 2 + csi_packet.HT40_GAP_SUBCARRIERS,
        _COMPRESSED_HT40_FIX32_CORRECTION,
        float((1 << COMPRESSED_HT40_FIX32_SHIFT) * 8.0),
    )
    gap_start = csi_packet.HT_COEFFICIENTS_PER_CHANNEL
    spectrum[gap_start : gap_start + csi_packet.HT40_GAP_SUBCARRIERS] = 0.0
    return spectrum


def decode_compressed_he20(buf) -> np.ndarray:
    spectrum = _decode_compressed_tap_window(
        buf,
        COMPRESSED_HE20_FFT_SIZE,
        COMPRESSED_HE20_TAP_START,
        COMPRESSED_TAP_COUNT,
        csi_packet.HE20_COEFFICIENTS_PER_CHANNEL,
        _COMPRESSED_HE20_FIX32_CORRECTION,
        float((1 << COMPRESSED_HE20_FIX32_SHIFT) * 8.0),
    )
    csi_processing.interpolate_he20ltf_gaps(spectrum)
    return spectrum
