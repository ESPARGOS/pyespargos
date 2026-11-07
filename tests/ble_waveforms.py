"""Independent deterministic GFSK fixtures for decoder consumers."""
import numpy as np

def waveform(address, *, phy=0, divider=8, cfo=30_000, noise=.02, corrupt=False, length=8):
    rate = 2_000_000 if phy else 1_000_000
    fs = 32_000_000 // divider
    sps = fs // rate
    advertising = address == 0x8e89bed6
    pdu = bytes([0x42 if advertising else 1, length]) + bytes(range(length))
    crc = 0xAAAAAA
    for byte in pdu:
        for bit in range(8):
            crc = (crc >> 1) ^ (0xDA6000 if (crc ^ (byte >> bit)) & 1 else 0)
    if corrupt:
        crc ^= 1
    data = pdu + crc.to_bytes(3, 'little')
    bits = np.unpackbits(np.frombuffer(data, np.uint8), bitorder='little')
    whitening = 37 | 64
    whitened = []
    for bit in bits:
        whitened.append(int(bit) ^ (whitening & 1))
        whitening = (whitening >> 1) ^ (0x44 if whitening & 1 else 0)
    preamble = np.tile([address & 1, 1 - (address & 1)], 8 if phy else 4)
    bits = np.r_[preamble, np.unpackbits(np.frombuffer(address.to_bytes(4, 'little'), np.uint8), bitorder='little'), whitened]
    t = np.arange(-sps, sps + 1)
    h = np.exp(-t * t / (2 * (.2625 * sps) ** 2))
    deviation = np.convolve(np.repeat(bits * 2. - 1, sps), h / h.sum(), mode='same')
    phase = -np.cumsum(deviation * 2 * np.pi * rate * .25 / fs)
    signal = np.exp(1j * (phase + 2 * np.pi * (2_000_000 + cfo) / fs * np.arange(len(phase))))
    signal = np.r_[np.zeros(40 * sps), signal, np.zeros(16 * fs // 1_000_000)]
    rng = np.random.default_rng(42)
    signal += noise * (rng.normal(size=len(signal)) + 1j * rng.normal(size=len(signal)))
    return signal * 150, pdu



def interop_cases():
    """Valid/invalid advertisements and unknown-address packets across rates."""
    for phy in (0, 1):
        for divider in ((1, 2, 4, 8) if phy == 0 else (1, 2, 4)):
            for address in (0x8e89bed6, 0x12345678):
                for corrupt in (False, True) if address == 0x8e89bed6 else (False,):
                    for cfo, dc in ((-100_000, 0), (0, 180 + 70j), (100_000, 0)):
                        z, pdu = waveform(address, phy=phy, divider=divider,
                                          corrupt=corrupt, cfo=cfo, noise=.04)
                        z = np.pad(z + dc, (0, -len(z) % 8))
                        raw = ((z.real.astype(np.int32) & 1023) |
                               ((z.imag.astype(np.int32) & 1023) << 10)).astype('<u4')
                        yield dict(raw=raw, pdu=pdu, address=address, phy=phy,
                                   divider=divider, valid=not corrupt)

    # Long data PDUs, truncation and noise also exercise parser rejection paths.
    for phy, divider in ((0, 8), (1, 4)):
        for length in (0, 1, 27, 251, 255):
            z, pdu = waveform(0x12345678, phy=phy, divider=divider, length=length)
            raw = ((z.real.astype(np.int32) & 1023) | ((z.imag.astype(np.int32) & 1023) << 10)).astype('<u4')
            yield dict(raw=raw, pdu=pdu, address=0x12345678, phy=phy, divider=divider, valid=True)
        z, _ = waveform(0x8e89bed6, phy=phy, divider=divider)
        raw = ((z.real.astype(np.int32) & 1023) | ((z.imag.astype(np.int32) & 1023) << 10)).astype('<u4')
        yield dict(raw=raw[:300], pdu=b'', address=0, phy=phy, divider=divider, valid=False)
        rng = np.random.default_rng(72)
        for _ in range(30):
            raw = ((rng.normal(0, 50, 16384).astype(np.int32) & 1023) | ((rng.normal(0, 50, 16384).astype(np.int32) & 1023) << 10)).astype('<u4')
            yield dict(raw=raw, pdu=b'', address=0, phy=phy, divider=divider, valid=False)
    for coding in (2, 8):
        for divider in (4, 8):
            for cfo in (-100000, 0, 100000):
                for corrupt in (False, True):
                    z, pdu = coded_waveform(coding=coding, divider=divider, cfo=cfo, noise=.04, corrupt=corrupt)
                    z = np.pad(z, (0, -len(z) % 8))
                    if len(z) > 16384:
                        continue
                    raw = ((z.real.astype(np.int32)&1023)|((z.imag.astype(np.int32)&1023)<<10)).astype('<u4')
                    yield dict(raw=raw, pdu=pdu, address=0x8e89bed6, phy=0, divider=divider, valid=not corrupt, coding=coding)
                    yield dict(raw=raw[:len(raw)//2], pdu=b'', address=0, phy=0, divider=divider, valid=False, coding=coding)

def coded_waveform(*, coding=8, divider=8, channel=37, cfo=30000, noise=.02, corrupt=False, length=12):
    """Independent BLE convolutional encoder, symbol mapper and GFSK transmitter."""
    address = 0x8e89bed6
    # ADV_EXT_IND with AdvA in its extended header.
    pdu = bytes([7, 8 + length, 7, 1]) + bytes.fromhex('70e5e5a0ed30') + bytes(range(length))
    crc = 0xaaaaaa
    for byte in pdu:
        for k in range(8):
            crc = (crc >> 1) ^ (0xda6000 if (crc ^ (byte >> k)) & 1 else 0)
    if corrupt:
        crc ^= 1
    bits = np.unpackbits(np.frombuffer(pdu + crc.to_bytes(3, 'little'), np.uint8), bitorder='little')
    whitening = channel | 64
    whitened = []
    for bit in bits:
        whitened.append(int(bit) ^ (whitening & 1))
        whitening = (whitening >> 1) ^ (0x44 if whitening & 1 else 0)
    def encode(data, spreading):
        # Shift register ordered newest to oldest, independent from decoder trellis.
        register = [0, 0, 0]
        output = []
        for bit in data:
            a = int(bit) ^ register[0] ^ register[1] ^ register[2]
            b = int(bit) ^ register[1] ^ register[2]
            register = [int(bit), *register[:2]]
            for value in (a, b):
                output.extend(([1, 1, 0, 0] if value else [0, 0, 1, 1]) if spreading == 8 else [value])
        return output
    first = list(np.unpackbits(np.frombuffer(address.to_bytes(4, 'little'), np.uint8), bitorder='little')) + [int(coding == 2), 0, 0, 0, 0]
    symbols = np.r_[np.tile([0,0,1,1,1,1,0,0],10), encode(first,8), encode(whitened + [0,0,0],coding)]
    fs = 32000000 // divider
    sps = fs // 1000000
    t = np.arange(-sps, sps+1)
    h = np.exp(-t*t/(2*(.2625*sps)**2))
    deviation = np.convolve(np.repeat(symbols*2.-1,sps),h/h.sum(),mode='same')
    phase = -np.cumsum(deviation*2*np.pi*250000/fs)
    signal = np.exp(1j*(phase+2*np.pi*(2000000+cfo)/fs*np.arange(len(phase))))
    signal = np.r_[np.zeros(40*sps),signal,np.zeros(16*sps)]
    rng = np.random.default_rng(42)
    signal += noise*(rng.normal(size=len(signal))+1j*rng.normal(size=len(signal)))
    return signal*150,pdu
