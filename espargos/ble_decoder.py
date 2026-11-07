"""Host BLE IQ demodulation using NumPy, with no native build dependency."""
from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class DecodedPacket:
    access_address: int
    packet_crc: int
    flags: int
    prefix: bytes
    coding: int = 0
    phy: int = 0


def _valid_address(aa):
    if aa == 0x8e89bed6:
        return True
    difference = aa ^ 0x8e89bed6
    if not difference & (difference - 1):
        return False
    transitions = upper = 0
    last, run = aa & 1, 1
    for i in range(1, 32):
        bit = (aa >> i) & 1
        if bit == last:
            run += 1
            if run > 6:
                return False
        else:
            transitions += 1
            upper += i >= 27
            run = 1
        last = bit
    return aa != (aa & 255) * 0x01010101 and transitions <= 24 and upper >= 2


def _crc(data):
    crc = 0xaaaaaa
    for byte in data:
        for bit in range(8):
            crc = (crc >> 1) ^ (0xda6000 if (crc ^ (byte >> bit)) & 1 else 0)
    return crc


def decode(raw, divider, channel, phy):
    """Decode packed ADC samples. Unknown connection CRC seeds remain unverified."""
    raw = np.asarray(raw, dtype=np.uint32)
    if raw.ndim != 1 or not 128 <= len(raw) <= 16384 or divider not in (1, 2, 4, 8) or not 0 <= channel <= 39 or phy not in (0, 1, 2):
        raise ValueError('Invalid BLE decoder input')
    target = 8 if phy == 1 else 4
    step = 32 // divider // target
    if not step:
        return None
    count = len(raw) // step
    preamble = 16 if phy == 1 else 8
    if count < (preamble + 72) * 4:
        return None
    samples = raw[:count * step:step]
    i = ((samples & 1023).astype(np.int64) ^ 512) - 512
    q = (((samples >> 10) & 1023).astype(np.int64) ^ 512) - 512
    rotation = np.arange(count) % (target // 2)
    if target == 4:
        sign = 1 - 2 * rotation
        i, q = i * sign, q * sign
    else:
        cosine = np.array([1, 0, -1, 0])[rotation]
        sine = np.array([0, -1, 0, 1])[rotation]
        i, q = i * cosine - q * sine, i * sine + q * cosine
    taps = np.ones(target // 2, dtype=np.int64)
    i, q = np.convolve(i, taps)[:count], np.convolve(q, taps)[:count]
    pi, pq = np.r_[0, i[:-1]], np.r_[0, q[:-1]]
    power = (i*i + q*q + pi*pi + pq*pq) // 2
    cross = pq*i - pi*q
    discriminator = np.trunc(np.divide(cross * 128, power, out=np.zeros(count), where=power != 0)).astype(np.int64) * 8
    work = np.trunc(np.convolve(discriminator, np.ones(4, dtype=np.int64))[:count] / 4).astype(np.int64)
    if phy != 1:
        coded = _decode_coded(work, channel)
        if coded is not None:
            return coded
        if phy == 2:
            return None
    # Evaluate all candidate timing phases together before parsing likely packets.
    starts = np.arange(8, min(64 * target, count - (preamble + 72) * 4))
    windows = work[starts[:, None] + np.arange(preamble) * 4]
    center = np.trunc(windows.mean(axis=1)).astype(np.int64)
    amplitude = np.trunc((windows * np.tile([-1, 1], preamble // 2)).mean(axis=1)).astype(np.int64)
    variance = (windows * windows).sum(axis=1) // preamble - center * center
    candidates = (abs(center) <= 400) & (abs(amplitude) <= 600) & (abs(amplitude) >= 80) & (variance > 0) & (amplitude * amplitude * 100 >= variance * 90)
    found = None
    for index in np.flatnonzero(candidates):
        begin = int(starts[index]) + preamble * 4
        threshold = int(center[index])
        bits = work[begin::4] > threshold
        aa = sum(int(bits[b]) << b for b in range(32))
        if (aa & 1) != (amplitude[index] < 0) or not _valid_address(aa):
            continue
        whitening, size, pdu = channel | 64, 2, bytearray()
        while len(pdu) < size + 3:
            byte = len(pdu)
            if begin + (32 + (byte + 1) * 8) * 4 >= count:
                break
            value = 0
            for bit in range(8):
                value |= (int(bits[32 + byte * 8 + bit]) ^ (whitening & 1)) << bit
                whitening = (whitening >> 1) ^ (0x44 if whitening & 1 else 0)
            pdu.append(value)
            if byte == 1:
                size = value + 2 + int(aa != 0x8e89bed6 and bool(pdu[0] & 0x20))
                if aa == 0x8e89bed6:
                    kind = pdu[0] & 15
                    if kind > 7 or pdu[0] & 16 or (kind != 7 and not 6 <= value <= 37):
                        break
                elif not pdu[0] & 3 or pdu[0] & 0xc0:
                    break
        if len(pdu) != size + 3:
            continue
        crc = int.from_bytes(pdu[size:size+3], 'little')
        advertising = aa == 0x8e89bed6
        if advertising:
            if _crc(pdu[:size]) != crc:
                continue
        else:
            n = 32 + (size + 3) * 8
            if count - (begin + n * 4) > 64 * target:
                continue
            error = abs(work[begin:begin+n*4:4] - threshold) - 330
            if np.sum(error * error) // n > 18000:
                continue
        packet = DecodedPacket(aa, crc, 3 if advertising else 1, bytes(pdu[:min(size, 32)]), phy=phy)
        if advertising:
            return packet
        if found is None:
            found = packet
    return found


# State bit zero is the newest stored input. Each destination has two predecessors.
_STATES = np.arange(8)
_PREDECESSORS = np.stack((_STATES >> 1, (_STATES >> 1) | 4), axis=1)
_INPUTS = _STATES[:, None] & 1
_G0 = _INPUTS ^ (_PREDECESSORS & 1) ^ ((_PREDECESSORS >> 1) & 1) ^ (_PREDECESSORS >> 2)
_G1 = _INPUTS ^ ((_PREDECESSORS >> 1) & 1) ^ (_PREDECESSORS >> 2)


def _viterbi(symbols, coding, count, terminated=True):
    """Soft-decision, eight-state BLE FEC with independent zero-state blocks."""
    if len(symbols) < count * coding:
        return None
    soft = np.asarray(symbols[:count*coding]).reshape(count, 2, coding // 2)
    if coding == 8:
        soft = (soft * np.array([1, 1, -1, -1])).sum(axis=2)
    else:
        soft = soft[:, :, 0]
    metrics = np.full(8, -np.inf)
    metrics[0] = 0
    paths = np.empty((count, 8), dtype=np.uint8)
    branches = soft[:, 0, None, None] * (2*_G0-1) + soft[:, 1, None, None] * (2*_G1-1)
    for n in range(count):
        scores = metrics[_PREDECESSORS] + branches[n]
        choice = scores[:, 1] > scores[:, 0]
        paths[n] = _PREDECESSORS[_STATES, choice.astype(int)]
        metrics = np.max(scores, axis=1)
    state = 0 if terminated else int(np.argmax(metrics))
    bits = np.empty(count, dtype=np.uint8)
    for n in range(count - 1, -1, -1):
        bits[n] = state & 1
        state = int(paths[n, state])
    return bits


def _dewhiten(bits, channel):
    whitening = channel | 64
    result = bytearray(len(bits) // 8)
    for k, bit in enumerate(bits[:len(result)*8]):
        result[k // 8] |= (int(bit) ^ (whitening & 1)) << (k % 8)
        whitening = (whitening >> 1) ^ (0x44 if whitening & 1 else 0)
    return result


def _decode_coded(work, channel):
    # LE Coded uses the same 1 Msymbol/s GFSK front end as LE1M.
    pattern = np.tile([-1, -1, 1, 1, 1, 1, -1, -1], 10)
    starts = np.arange(8, min(256, len(work) - (376 + 43*2)*4))
    windows = work[starts[:, None] + np.arange(80)*4]
    centers = windows.mean(axis=1)
    amplitudes = (windows * pattern).mean(axis=1)
    variance = (windows*windows).mean(axis=1) - centers*centers
    eligible = (abs(centers) <= 400) & (amplitudes >= 80) & (amplitudes <= 600) & (amplitudes**2 >= variance*.85)
    for index in np.flatnonzero(eligible):
        start, center = int(starts[index]), centers[index]
        symbols = work[start+80*4::4] - center
        first = _viterbi(symbols, 8, 37)
        if first is None:
            continue
        aa = int.from_bytes(np.packbits(first[:32], bitorder='little').tobytes(), 'little')
        ci = int(first[32]) | int(first[33]) << 1
        if ci > 1 or not _valid_address(aa):
            continue
        coding = 2 if ci else 8
        symbols = symbols[37*8:]
        header = _viterbi(symbols, coding, 32, terminated=False)
        if header is None:
            continue
        header = _dewhiten(header[:16], channel)
        size = header[1] + 2
        if aa == 0x8e89bed6:
            if header[0] & 15 != 7 or header[0] & 16:
                continue
        elif not header[0] & 3 or header[0] & 0xe0:
            continue
        count = (size + 3)*8 + 3
        bits = _viterbi(symbols, coding, count)
        if bits is None:
            continue
        pdu = _dewhiten(bits[:-3], channel)
        if pdu[:2] != header:
            continue
        crc = int.from_bytes(pdu[size:size+3], 'little')
        advertising = aa == 0x8e89bed6
        if advertising and _crc(pdu[:size]) != crc:
            continue
        if not advertising:
            # Without a known CRC seed, require the declared end at the burst tail.
            if len(symbols) - count*coding > 64:
                continue
            # Re-encode the decoded path to reject noise that merely fits a header.
            state, encoded = 0, []
            for bit in bits:
                a = int(bit) ^ (state & 1) ^ ((state >> 1) & 1) ^ (state >> 2)
                b = int(bit) ^ ((state >> 1) & 1) ^ (state >> 2)
                state = ((state << 1) | int(bit)) & 7
                for value in (a, b):
                    encoded.extend(([1, 1, -1, -1] if value else [-1, -1, 1, 1]) if coding == 8 else [2*value-1])
            if np.mean(symbols[:count*coding] * encoded) < 180:
                continue
        return DecodedPacket(aa, crc, 3 if advertising else 1, bytes(pdu[:min(size, 32)]), coding, 2)
    return None
