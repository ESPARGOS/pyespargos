"""ADC sample encoding shared by the WiFi and BLE packet receivers."""

import numpy as np


def unpack_words(data, offset, packed):
    if not packed:
        return np.frombuffer(data, "<u4", offset=offset)
    # Two 20-bit words share five consecutive little-endian bytes.
    b = np.frombuffer(data, np.uint8, offset=offset).reshape(-1, 5).astype(np.uint32)
    raw = np.empty(2 * len(b), np.uint32)
    raw[::2] = b[:, 0] | b[:, 1] << 8 | (b[:, 2] & 15) << 16
    raw[1::2] = b[:, 2] >> 4 | b[:, 3] << 4 | b[:, 4] << 12
    return raw


def complex_samples(raw, *, conjugate=False):
    i = ((raw & 1023).astype(np.int32) ^ 512) - 512
    q = (((raw >> 10) & 1023).astype(np.int32) ^ 512) - 512
    return (i + (-1j if conjugate else 1j) * q).astype(np.complex64)
