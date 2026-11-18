"""Host-side encoders for low-bandwidth IQ transmission."""

import numpy as np

# ITU-R M.2034 (2013), Annex 2, ASCII Varicode code points:
# https://www.itu.int/rec/R-REC-M.2034/en
_VARICODE = (
    "1010101011 1011011011 1011101101 1101110111 1011101011 1101011111 1011101111 1011111101 "
    "1011111111 11101111 11101 1101101111 1011011101 11111 1101110101 1110101011 "
    "1011110111 1011110101 1110101101 1110101111 1101011011 1101101011 1101101101 1101010111 "
    "1101111011 1101111101 1110110111 1101010101 1101011101 1110111011 1011111011 1101111111 "
    "1 111111111 101011111 111110101 111011011 1011010101 1010111011 101111111 "
    "11111011 11110111 101101111 111011111 1110101 110101 1010111 110101111 "
    "10110111 10111101 11101101 11111111 101110111 101011011 101101011 110101101 "
    "110101011 110110111 11110101 110111101 111101101 1010101 111010111 1010101111 "
    "1010111101 1111101 11101011 10101101 10110101 1110111 11011011 11111101 "
    "101010101 1111111 111111101 101111101 11010111 10111011 11011101 10101011 "
    "11010101 111011101 10101111 1101111 1101101 101010111 110110101 101011101 "
    "101110101 101111011 1010101101 111110111 111101111 111111011 1010111111 101101101 "
    "1011011111 1011 1011111 101111 101101 11 111101 1011011 "
    "101011 1101 111101011 10111111 11011 111011 1111 111 "
    "111111 110111111 10101 10111 101 110111 1111011 1101011 "
    "11011111 1011101 111010101 1010110111 110111011 1010110101 1011010111 1110110101 "
).split()


def psk31_bits(text, *, preamble=32, postamble=32):
    """Encode ASCII text as differential BPSK31 bits (before index mapping).

    Zero bits reverse phase. The default idle preamble permits acquisition;
    the steady-carrier postamble lets decoders squelch. For successive pieces
    of a conversation, use a preamble only on the first piece and a postamble
    only on the last; character separators are included in every piece.
    """
    if not isinstance(preamble, int) or not isinstance(postamble, int) or min(preamble, postamble) < 0:
        raise ValueError("preamble and postamble must be nonnegative bit counts")
    data = text.encode("ascii") if isinstance(text, str) else bytes(text)
    if any(value > 127 for value in data):
        raise ValueError("BPSK31 text encoder supports ASCII")
    symbols = "0" * preamble + "".join(_VARICODE[value] + "00" for value in data) + "1" * postamble
    return bytes(int(bit) for bit in symbols)


class SSBEncoder:
    """Stateful audio-to-IQ conversion for the raw streaming mode.

    Feed normalized, zero-mean real audio at the raw stream's sample rate.
    A 257-tap windowed Hilbert filter suppresses the other sideband without
    discontinuities between calls. The useful voice band starts around
    300 Hz; allow margin below Nyquist. Group delay is 128 audio samples.
    The default gain reserves headroom for the quadrature filter's overshoot.
    """

    def __init__(self, *, sideband="upper", gain=0.25):
        if sideband not in ("upper", "lower"):
            raise ValueError("sideband must be upper or lower")
        if not np.isfinite(gain) or not 0 < gain <= 1:
            raise ValueError("gain must be finite and in (0, 1]")
        n = np.arange(-128, 129)
        h = np.zeros(257)
        odd = n % 2 != 0
        h[odd] = 2 / (np.pi * n[odd])
        self._filter = h * np.blackman(257)
        self._history = np.zeros(256)
        self._sign = 1 if sideband == "upper" else -1
        self.gain = gain

    def encode(self, audio):
        """Return one complex IQ sample per audio sample, retaining FIR history."""
        a = np.asarray(audio)
        if a.ndim != 1 or np.iscomplexobj(a) or not np.all(np.isfinite(a)) or np.any(abs(a) > 1):
            raise ValueError("audio must be a finite real vector within [-1, 1]")
        if not len(a):
            return np.empty(0, complex)
        joined = np.concatenate((self._history, a))
        q = np.convolve(joined, self._filter, mode="valid")
        iq = self.gain * (joined[128 : 128 + len(a)] + 1j * self._sign * q)
        if np.any(abs(iq.imag) > 1):
            raise ValueError("quadrature audio exceeds CS8 range; reduce encoder gain")
        self._history = joined[-256:].copy()
        return iq

    def flush(self):
        """Return the filter tail; feed it before finishing the IQ stream."""
        return self.encode(np.zeros(256))


class SingleCarrier:
    """Indexed constellation and finite complex pulse, sampled per symbol.

    Pulse sample zero is the symbol's time origin. ``pulse`` contains
    ``span * samples_per_symbol + 1`` coefficients, including both endpoints.
    Missing symbols contribute zero without consuming a constellation index.
    """

    def __init__(self, constellation, pulse, *, symbol_rate_hz, samples_per_symbol=32):
        import struct

        c = np.asarray(constellation, dtype=complex)
        p = np.asarray(pulse, dtype=complex)
        phases = samples_per_symbol
        if c.ndim != 1 or not 1 <= len(c) <= 64 or not np.all(np.isfinite(c)) or np.any(abs(c) > 1 + 1e-12):
            raise ValueError("Use 1..64 finite constellation points of magnitude <=1")
        if phases not in (16, 32, 64, 128) or p.ndim != 1 or (len(p) - 1) % phases or not 1 < len(p) <= 8 * phases + 1:
            raise ValueError("Pulse needs span * samples_per_symbol + 1 samples; span 1..8")
        span = (len(p) - 1) // phases
        if not np.all(np.isfinite(p)) or np.any(abs(p) > 1 + 1e-12):
            raise ValueError("Pulse coefficients must be finite with magnitude <=1")
        if not np.isfinite(symbol_rate_hz) or not 1 <= symbol_rate_hz <= 1000000:
            raise ValueError("Use a symbol rate from 1 to 1000000 baud")
        if (
            max(
                sum(abs(p[k * phases + phase]) for k in range(span))
                for phase in range(phases + 1)
            )
            > 1.899
        ):
            raise ValueError("Overlapping pulse magnitudes must sum to <=1.899")
        period = round(16_000_000 * 256 / symbol_rate_hz)
        # Above the general renderer's limit, timing must fit the bounded
        # sensor pulse bank. Include every phase visited by its output grid.
        import math

        states = len(c) if span == 1 else (len(c) + 1) ** span
        step = 256 if period < 16384 else 8192
        bank_bytes = period // math.gcd(period, step) * states * 4
        if symbol_rate_hz > (32000 if span == 2 else 16000) and bank_bytes > (
            8 * 1024 if period < 16384 else 256 * 1024
        ):
            raise ValueError(
                "Symbol timing exceeds the pulse bank (8 KiB native, 256 KiB staged); lower the rate or shorten the pulse"
            )

        def iq16(v):
            return (
                np.column_stack((v.real, v.imag))
                .__mul__(32767)
                .round()
                .astype("<i2")
                .tobytes()
            )

        self.payload = (
            struct.pack("<I4H", period, phases, span, len(c), 0) + iq16(c) + iq16(p)
        )
        self.symbol_rate_hz = 16_000_000 * 256 / period
        self.constellation = c.copy()
        self.pulse = p.copy()
        self.samples_per_symbol = phases

    @classmethod
    def from_payload(cls, payload):
        """Load the controller's exact Q15 table without requantizing it."""
        import math
        import struct

        data = bytes(payload)
        if len(data) < 12:
            raise ValueError("Incomplete single-carrier table")
        period, phases, span, count, reserved = struct.unpack_from("<I4H", data)
        if (
            not 4096 <= period <= 4096000000
            or phases not in (16, 32, 64, 128)
            or not 1 <= span <= 8
            or not 1 <= count <= 64
            or reserved != 0
            or len(data) != 12 + 4 * (count + span * phases + 1)
        ):
            raise ValueError("Invalid single-carrier table")
        pairs = np.frombuffer(data, dtype="<i2", offset=12).reshape(-1, 2).astype(float)
        points, pulse = pairs[:count], pairs[count:]
        if (
            np.any(np.hypot(points[:, 0], points[:, 1]) > 32768)
            or np.any(np.hypot(pulse[:, 0], pulse[:, 1]) > 32768)
            or max(
                sum(np.linalg.norm(pulse[k * phases + phase]) for k in range(span))
                for phase in range(phases + 1)
            )
            > 1.9 * 32768
            or (
                period < (128000 if span == 2 else 256000)
                and 4
                * (count if span == 1 else (count + 1) ** span)
                * (period // math.gcd(period, 256 if period < 16384 else 8192))
                > (8 * 1024 if period < 16384 else 256 * 1024)
            )
        ):
            raise ValueError("Invalid constellation or pulse coefficients")
        result = cls.__new__(cls)
        result.payload = data
        result.symbol_rate_hz = 4096000000 / period
        result.samples_per_symbol = phases
        result.constellation = (points[:, 0] + 1j * points[:, 1]) / 32768
        result.pulse = (pulse[:, 0] + 1j * pulse[:, 1]) / 32768
        return result

    @classmethod
    def psk31(cls):
        """BPSK31: absolute signs with a two-symbol raised-cosine pulse."""
        t = np.arange(65) / 32
        return cls([-1, 1], 0.5 * (1 - np.cos(np.pi * t)), symbol_rate_hz=31.25)


def psk31_symbols(text, *, preamble=32, postamble=32):
    """Encode one complete PSK31 burst as absolute constellation indices.

    Use with ``SingleCarrier.psk31()``. Differential mapping is host-side;
    packet loss therefore cannot corrupt the phase of later symbols.
    """
    sign = 1
    result = bytearray([sign])
    for bit in psk31_bits(text, preamble=preamble, postamble=postamble):
        sign ^= not bit
        result.append(sign)
    return bytes(result)


class GFSK:
    """Packet GFSK synthesized on sensors; LE 1M parameters by default.

    Supply complete air bits to IQStream.write_packet(). Symbol rates must
    divide 16 MSa/s, from 125 kbaud to 1 Mbaud. Modulation index is specified
    in sixteenths. Packets reset phase deterministically and do not share FIR
    history, so dropped packets cannot corrupt their successors.
    """

    def __init__(
        self, *, symbol_rate_hz=1_000_000, bt=0.5, modulation_index=0.5, span=4
    ):
        import struct

        if not np.isfinite(symbol_rate_hz) or not 125000 <= symbol_rate_hz <= 1000000:
            raise ValueError("GFSK rate must be between 125000 and 1000000 baud")
        samples = round(16_000_000 / symbol_rate_hz)
        if abs(samples * symbol_rate_hz - 16_000_000) > 0.001:
            raise ValueError("GFSK rate must divide 16 MSa/s exactly")
        if not np.isfinite(bt) or not 0.2 <= bt <= 1 or span not in (2, 4):
            raise ValueError("Use BT from 0.2 to 1 and a span of 2 or 4 symbols")
        if (
            not np.isfinite(modulation_index)
            or not 1 <= modulation_index * 16 <= 16
            or modulation_index * 16 != round(modulation_index * 16)
        ):
            raise ValueError(
                "Modulation index must be 1/16 through 1, in steps of 1/16"
            )
        import math

        span = int(span)
        states = (32 // math.gcd(round(modulation_index * 16), 32)) * (1 << (span + 1))
        if states * samples * 4 > 8 * 1024:
            raise ValueError("GFSK configuration exceeds the 8 KiB native pulse bank")
        self.symbol_rate_hz = 16_000_000 / samples
        self.bt = float(bt)
        self.modulation_index = float(modulation_index)
        self.span = int(span)
        self.payload = struct.pack(
            "<IfHHI", samples * 256, self.bt, span, round(modulation_index * 16), 0
        )

    @classmethod
    def le1m(cls):
        return cls()

    @classmethod
    def from_payload(cls, payload):
        import struct

        if len(payload) != 16:
            raise ValueError("Invalid GFSK configuration length")
        period, bt, span, index, reserved = struct.unpack("<IfHHI", payload)
        if reserved or not period or period % 256:
            raise ValueError("Invalid GFSK configuration")
        return cls(
            symbol_rate_hz=4096000000 / period,
            bt=bt,
            span=span,
            modulation_index=index / 16,
        )


def ble_le1m_packet(pdu, *, channel=37, access_address=0x8E89BED6, crc_init=0x555555):
    """Build LE 1M air bytes from a PDU, including CRC and whitening.

    Defaults are for advertising. Data-channel PDUs need their connection's
    access address and CRC initializer. Bit order is LSB first in each byte.
    This helper does not implement connection scheduling or channel hopping.
    """
    data = bytes(pdu)
    if not 2 <= len(data) <= 257:
        raise ValueError("Use a complete BLE PDU of 2..257 bytes")
    if not isinstance(channel, int) or not 0 <= channel <= 39:
        raise ValueError("BLE channel must be 0..39")
    if (
        not isinstance(access_address, int)
        or not 0 <= access_address < 2**32
        or not isinstance(crc_init, int)
        or not 0 <= crc_init < 2**24
    ):
        raise ValueError("Invalid access address or CRC initializer")
    crc = int(f"{crc_init:024b}"[::-1], 2)
    for byte in data:
        for k in range(8):
            crc = (crc >> 1) ^ (0xDA6000 if (crc ^ (byte >> k)) & 1 else 0)
    whitening = channel | 64
    encoded = bytearray()
    for byte in data + crc.to_bytes(3, "little"):
        out = 0
        for k in range(8):
            out |= (((byte >> k) ^ whitening) & 1) << k
            whitening = (whitening >> 1) ^ (0x44 if whitening & 1 else 0)
        encoded.append(out)
    return (
        bytes([0x55 if access_address & 1 else 0xAA])
        + access_address.to_bytes(4, "little")
        + encoded
    )
