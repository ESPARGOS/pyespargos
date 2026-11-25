"""Refine legacy training CSI with verified OFDM signaling and payload symbols."""

import numpy as np

from ..csi_processing import interpolate_lltf_gap

MODES = ("training", "headers", "payload")
_TONES = np.r_[-26:0, 1:27]
_BINS = _TONES % 64
_PILOTS = np.isin(_TONES, [-21, -7, 7, 21])
_PILOT_SIGNS = np.array([1, 1, 1, -1])
_POLARITY = np.array([1, 1, 1, 1, -1, -1, -1, 1, -1, -1, -1])
_INTERLEAVER = np.arange(48) // 16 + 3 * (np.arange(48) % 16)
_RATES = {13: 6, 15: 9, 5: 12, 7: 18, 9: 24, 1: 48, 11: 36, 3: 54}
_BPSC = {6: 1, 9: 1, 12: 2, 18: 2, 24: 4, 36: 4, 48: 6, 54: 6}
# HT-LTF signs on the 52 tones shared with the legacy channel.
_HT_LTF = np.array([1, 1, -1, -1, 1, 1, -1, 1, -1, 1, 1, 1, 1, 1, 1, -1, -1, 1, 1, -1, 1, -1, 1, 1, 1, 1, 1, -1, -1, 1, 1, -1, 1, -1, 1, -1, -1, -1, -1, -1, 1, 1, -1, -1, 1, -1, 1, -1, 1, 1, 1, 1])


def _bits(bits, start, count):
    return sum(int(v) << i for i, v in enumerate(bits[start : start + count]))


def _codeword(symbols):
    """Invert the rate-1/2 convolutional code and check both outputs and termination."""
    state = 0
    bits = []
    for symbol in symbols:
        coded = (symbol[~_PILOTS].real > 0)[_INTERLEAVER]
        for first, second in zip(coded[::2], coded[1::2]):
            bit = int(first) ^ (((state << 1) & 0o155).bit_count() & 1)
            state = ((state << 1) | bit) & 127
            if ((state & 0o117).bit_count() & 1) != second:
                return None
            bits.append(bit)
    return bits if not any(bits[-6:]) else None


class _Observation:
    def __init__(self, packet):
        self.packet = packet
        self.start, self.cfo, self.gain, h = packet.ofdm
        self.dc = packet.samples[self.start : self.start + 128].mean()
        self.seed = h[_TONES + 26]
        self.total = 2 * self.seed.copy()
        self.weight = np.full(52, 2.0)
        self.mapping = 1

    def spectrum(self, offset, size=64, *, gain=None):
        p = self.packet
        start = self.start + offset
        end = start + size
        if end > p.raw.size or self.gain is None:
            return None
        expected = self.gain if gain is None else gain
        if p._training_gain(start, end) != expected:
            return None
        x = p.valid_samples(start, end)
        if x is None:
            return None
        dc = self.dc if expected == self.gain else x.mean()
        return np.fft.fft((x - dc) * np.exp(-2j * np.pi * self.cfo / p.sample_rate * np.arange(offset, offset + size)))

    def fft(self, offset):
        y = self.spectrum(offset)
        return None if y is None else y[_BINS]

    def signal(self, offset, rotated=False):
        y = self.fft(offset)
        if y is None:
            return None
        cross = y * self.seed.conj()
        phase = np.angle(np.sum(cross[_PILOTS] * _PILOT_SIGNS))
        y *= np.exp(-1j * phase)
        symbols = np.ones(52, complex)
        symbols[_PILOTS] = _PILOT_SIGNS
        axis = 1j if rotated else 1
        symbols[~_PILOTS] = np.where((y * (self.seed * axis).conj())[~_PILOTS].real > 0, axis, -axis)
        measured = y / symbols
        if np.sum(abs(measured - self.seed) ** 2) > 0.15 * np.sum(abs(self.seed) ** 2):
            return None
        return symbols / axis, measured

    def headers(self, payload):
        p = self.packet
        sig = self.signal(144)
        bits = _codeword([sig[0]]) if sig else None
        if not bits or bits[4] or sum(bits[:18]) % 2:
            return None
        rate = _RATES.get(int("".join(map(str, bits[:4])), 2))
        if not rate:
            return None
        if p.phy == 0 and (2 * rate != p.half_rate or _bits(bits, 5, 12) != p.length):
            return None
        self.total += sig[1]
        self.weight += 1
        if p.phy == 0:
            return (224, 80, _BPSC[rate], 4 * rate, False)
        if p.phy != 2:
            return None
        ht = [self.signal(offset, True) for offset in (224, 304)]
        bits = _codeword([field[0] for field in ht]) if all(ht) else None
        if not bits or not bits[26] or _bits(bits, 8, 16) != p.length:
            return None
        crc = 255
        for bit in bits[:34]:
            crc = ((crc << 1) & 255) ^ (7 if (crc >> 7) ^ bit else 0)
        if any(bits[34 + i] != (1 ^ ((crc >> (7 - i)) & 1)) for i in range(8)):
            return None
        self.total += ht[0][1] + ht[1][1]
        self.weight += 2
        mcs = _bits(bits, 0, 7)
        if not payload or mcs > 7 or bits[7] or any(bits[28:31]) or any(bits[32:34]):
            return None
        training = self.fft(464)
        if training is None:
            return None
        # A single-stream HT preamble may differ by a constant phase and
        # amplitude. Reject frequency-dependent changes of spatial mapping.
        power = np.vdot(self.seed, self.seed).real
        if power <= 0:
            return None
        ht_channel = training * _HT_LTF
        self.mapping = np.vdot(self.seed, ht_channel) / power
        if abs(self.mapping) < 0.1 or np.sum(abs(ht_channel / self.mapping - self.seed) ** 2) > 0.04 * power:
            return None
        return (536 if bits[31] else 544, 72 if bits[31] else 80, (1, 2, 2, 4, 4, 6, 6, 6)[mcs], (26, 52, 78, 104, 156, 208, 234, 260)[mcs], True)


def _payload(rows, plan):
    first, stride, bpsc, dbps, ht = plan
    scale = (1, np.sqrt(2), 0, np.sqrt(10), 0, np.sqrt(42))[bpsc - 1]
    max_level = 1 if bpsc == 1 else (1 << (bpsc // 2)) - 1

    def nearest(x):
        return np.clip(2 * np.rint((x * scale + max_level) / 2) - max_level, -max_level, max_level) / scale

    length = rows[0].packet.length
    for n in range(min(8, (16 + 8 * length + 6 + dbps - 1) // dbps)):
        available = [(r, y) for r in rows if (y := r.fft(first + n * stride)) is not None]
        if len(available) < 3:
            continue
        h = np.array([r.total / r.weight for r, y in available])
        y = np.array([y / r.mapping for r, y in available])
        cross = h.conj() * y
        power = abs(h) ** 2
        # Decide each transmitted point using only the other antennas.
        den = power.sum(axis=0) - power
        z = (cross.sum(axis=0) - cross) / np.maximum(den, 1e-12)
        pilots = np.roll(_PILOT_SIGNS, -n % 4) if ht else _PILOT_SIGNS
        pilots = pilots * _POLARITY[n + (3 if ht else 1)]
        phase = np.exp(-1j * np.angle((z[:, _PILOTS] * pilots).sum(axis=1)))[:, None]
        z *= phase
        template = nearest(z.real) + (1j * nearest(z.imag) if bpsc > 1 else 0)
        template[:, _PILOTS] = pilots
        good = (den > 0) & (abs(z - template) < 0.4 / scale)
        for i, (r, _) in enumerate(available):
            if not good[i, _PILOTS].all() or good[i].sum() < 40:
                continue
            t = template[i, good[i]]
            measured = y[i, good[i]] * phase[i, 0]
            expected = h[i, good[i]] * t
            if np.sum(abs(measured - expected) ** 2) > 0.15 * np.sum(abs(expected) ** 2):
                continue
            precision = abs(r.mapping) ** 2
            r.total[good[i]] += precision * measured * t.conj()
            r.weight[good[i]] += precision * abs(t) ** 2


def refine_channels(packets, mode, channels):
    rows = {pos: _Observation(p) for pos, p in packets.items()}
    usable = {}
    for row in rows.values():
        plan = row.headers(mode == "payload")
        if mode == "payload" and plan:
            usable.setdefault(plan, []).append(row)
    if len(usable) == 1:
        plan, receivers = next(iter(usable.items()))
        if len(receivers) >= 3:
            _payload(receivers, plan)
    result = channels.copy()
    for pos, r in rows.items():
        result[pos][_TONES + 26] = r.total / r.weight
        interpolate_lltf_gap(result[pos])
    return result
