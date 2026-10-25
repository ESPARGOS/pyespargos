"""Reproduce the calibration waveforms synthesized by sensor firmware.

Sensors receive a compact pilot recipe and generate samples in their replay
buffers. This Python model mirrors their integer arithmetic so captured spectra
can be compared with the transmitted waveforms. It is shared by monitor-based
TX calibration and TX–RX delay measurement, and performs no device I/O.
"""

import numpy as np


def pilots(n=1024, peak=192, variant=0, mask=None):
    """Reproduce the calibration pilots generated locally by sensor firmware.

    The calibration command asks each sensor to synthesize a periodic waveform
    in its replay buffer. This function builds the corresponding Python model;
    it neither uploads samples nor communicates with a device. It mirrors the
    firmware's integer inverse FFT and sample rounding so those effects are
    included in the reference spectrum used to estimate phase and delay.

    Return eight ``(bins, spectrum, samples)`` tuples, one per pilot slot.
    ``n`` is the waveform length and ``peak`` sets the largest integer I or Q
    component. Slots normally occupy disjoint sets of frequency bins, allowing
    simultaneous transmitters to be separated in one monitor capture. ``variant``
    changes the pilot phases; ``mask`` selects a combination of slots and returns
    that same combined waveform for all eight entries.
    """
    quarter = np.minimum(32767, np.floor(32767 * np.sin(np.arange(1025) * 2 * np.pi / 4096) + 0.5)).astype(np.int64)

    def sine(p):
        p = p % 4096
        quadrant, x = p // 1024, p % 1024
        return quarter[np.where(quadrant & 1, 1024 - x, x)] * np.where(quadrant >= 2, -1, 1)

    bins = np.array([k for k in range(-n * 3 // 8, n * 3 // 8 + 1) if abs(k) >= 8])
    owner = np.where(abs(bins) == n // 16 + 1, 0, abs(bins) % 8)
    result = []
    for slot in range(8 if mask is None else 1):
        k = bins[owner == slot] if mask is None else bins[(mask & (1 << owner)) != 0]
        phase = ((k * k * (37 + 11 * variant) + 190 * k) * 4096 + 1000) // 2000
        z = np.zeros((n, 2), dtype=np.int64)
        z[k % n, 0], z[k % n, 1] = sine(phase + 1024), sine(phase)
        j = 0
        for i in range(1, n):
            bit = n >> 1
            while j & bit:
                j ^= bit
                bit >>= 1
            j ^= bit
            if i < j:
                z[[i, j]] = z[[j, i]]
        length = 2
        while length <= n:
            for b in range(length // 2):
                r, q = sine(b * 4096 // length + 1024), sine(b * 4096 // length)
                x = np.arange(b, n, length)
                y = x + length // 2
                re = (r * z[y, 0] - q * z[y, 1] + 16384) // 32768
                im = (r * z[y, 1] + q * z[y, 0] + 16384) // 32768
                delta = np.column_stack((re, im))
                z[y] = z[x] - delta
                z[x] += delta
            length *= 2
        scale = max(abs(z).max(), 1)
        z = (z * peak + scale // 2) // scale
        z = z[:, 0] + 1j * z[:, 1]
        result.append((k, np.fft.fft(z), z))
    # A mask selects the same pilot on every sensor.
    if mask is not None:
        k, spectrum, samples = result[0]
        result = [(k.copy(), spectrum.copy(), samples.copy()) for _ in range(8)]
    return result
