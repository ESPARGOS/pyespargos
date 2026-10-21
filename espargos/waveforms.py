"""Source waveforms for sensor synthesis and offline sample generation."""

from dataclasses import dataclass
from pathlib import Path
import math

import numpy as np

__all__ = ["Waveform", "CW", "TwoTone", "LFM", "Noise", "QPSK", "IQSamples"]

SAMPLE_RATE = 80_000_000


def _finite(value, name):
    if not isinstance(value, (int, float, np.number)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


class Waveform:
    """A source waveform, independent of amplitude and antenna selection."""

    def synthesize(self, samples=1024):
        """Return complex samples at 80 MSa/s, before amplitude and steering.

        Uses the sensor's frequency rounding, Q15 sine table and 10-bit
        quantization. No device connection is required.
        """
        if not isinstance(samples, int) or not 256 <= samples <= 8192 or samples & (samples - 1):
            raise ValueError("samples must be a power of two from 256 to 8192")
        recipe = self.recipe(samples)
        if isinstance(self, IQSamples):
            i = (self._words & 1023).astype(np.int32)
            q = ((self._words >> 10) & 1023).astype(np.int32)
            return ((i ^ 512) - 512 + 1j * ((q ^ 512) - 512)) / 511
        t = np.arange(samples, dtype=np.int64)
        quarter = np.floor(32767 * np.sin(np.arange(1025) * np.pi / 2048) + 0.5).astype(np.int64)

        def sine(phase):
            phase = np.asarray(phase) & 4095
            quadrant, x = phase >> 10, phase & 1023
            value = quarter[np.where(quadrant & 1, 1024 - x, x)]
            return np.where(quadrant >= 2, -value, value)

        def bin_for(hz):
            return math.floor(hz * samples / SAMPLE_RATE + 0.5)

        kind = recipe["type"]
        if kind in ("noise", "qpsk"):
            seed = recipe["seed"]
            z = np.empty((samples, 2), dtype=np.int64)
            for k in range(samples):
                if kind == "noise":
                    for c in range(2):
                        seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
                        z[k, c] = (seed >> 16) - 32768
                else:
                    if k % 8 == 0:
                        seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
                    phase = 512 + (seed >> 30) * 1024
                    z[k] = sine(phase + 1024), sine(phase)
        else:
            num, den = bin_for(recipe.get("first_hz", recipe.get("frequency_hz", 0))) * t * 4096, samples
            if kind == "lfm":
                local = (t - recipe["offset_samples"]) % samples
                length = recipe["chirp_samples"]
                start, stop = bin_for(recipe["start_hz"]), bin_for(recipe["stop_hz"])
                num = (2 * start * length * local + (stop - start) * local * local) * 2048
                den = samples * length
            phase = (num + den // 2) // den
            z = np.column_stack((sine(phase + 1024), sine(phase)))
            if kind == "two-tone":
                phase2 = (bin_for(recipe["second_hz"]) * t * 4096 + den // 2) // den
                z += np.column_stack((sine(phase2 + 1024), sine(phase2)))
            if kind == "lfm":
                z[local >= length] = 0
        peak = max(1, int(np.abs(z).max()))
        z = (z * 511 * 65536 + peak * 32768) // (peak * 65536)
        return (z[:, 0] + 1j * z[:, 1]) / 511

    def recipe(self, samples):
        raise NotImplementedError


@dataclass(frozen=True)
class CW(Waveform):
    offset_hz: float = 1e6

    def recipe(self, samples):
        if abs(_finite(self.offset_hz, "offset_hz")) > SAMPLE_RATE / 2:
            raise ValueError("CW offset must lie within ±40 MHz")
        return dict(type="cw", samples=samples, frequency_hz=self.offset_hz)


@dataclass(frozen=True)
class TwoTone(Waveform):
    first_hz: float = -1e6
    second_hz: float = 1e6

    def recipe(self, samples):
        for name in ("first_hz", "second_hz"):
            if abs(_finite(getattr(self, name), name)) > SAMPLE_RATE / 2:
                raise ValueError("Tone offsets must lie within ±40 MHz")
        return dict(type="two-tone", samples=samples, first_hz=self.first_hz, second_hz=self.second_hz)


@dataclass(frozen=True)
class LFM(Waveform):
    start_hz: float = -40e6
    stop_hz: float = 40e6
    duration_s: float = 0.4e-6
    delay_s: float = 0

    def recipe(self, samples):
        for name in ("start_hz", "stop_hz"):
            if abs(_finite(getattr(self, name), name)) > SAMPLE_RATE / 2:
                raise ValueError("Chirp frequencies must lie within ±40 MHz")
        active = _finite(self.duration_s, "duration_s") * SAMPLE_RATE
        offset = _finite(self.delay_s, "delay_s") * SAMPLE_RATE
        if abs(active - round(active)) > 1e-6 or abs(offset - round(offset)) > 1e-6:
            raise ValueError("Chirp timing must use whole samples (12.5 ns)")
        active, offset = round(active), round(offset)
        if active < 1 or offset < 0 or active + offset > samples:
            raise ValueError("Chirp duration and delay must fit inside the replay buffer")
        return dict(type="lfm", samples=samples, start_hz=self.start_hz, stop_hz=self.stop_hz, chirp_samples=active, pause_samples=samples - active, offset_samples=offset)


@dataclass(frozen=True)
class Noise(Waveform):
    seed: int = 0x657370

    def recipe(self, samples):
        if not isinstance(self.seed, int) or not 0 <= self.seed <= 0xFFFFFFFF:
            raise ValueError("seed must be a uint32")
        return dict(type="noise", samples=samples, seed=self.seed)


@dataclass(frozen=True)
class QPSK(Noise):
    def recipe(self, samples):
        return {**super().recipe(samples), "type": "qpsk"}


class IQSamples(Waveform):
    """Complex samples in full-scale units. Values must fit within ±1 per I/Q.

    Samples are copied and quantized once. No automatic normalization is applied.
    """

    def __init__(self, samples):
        data = np.asarray(samples)
        if data.ndim != 1 or not np.iscomplexobj(data) or not np.isfinite(data).all():
            raise ValueError("Expected a finite, one-dimensional complex array")
        if np.any(abs(data.real) > 1) or np.any(abs(data.imag) > 1):
            raise ValueError("I and Q must lie within ±1")
        i, q = np.rint(data.real * 511).astype(np.int32), np.rint(data.imag * 511).astype(np.int32)
        self._words = ((i & 1023) | ((q & 1023) << 10)).astype("<u4")
        self._words.flags.writeable = False

    @classmethod
    def from_file(cls, path, *, normalize=False):
        data = np.load(path, allow_pickle=False) if Path(path).suffix.lower() == ".npy" else np.loadtxt(path, delimiter="," if Path(path).suffix.lower() == ".csv" else None)
        if data.ndim == 2 and data.shape[1] == 2:
            data = data[:, 0] + 1j * data[:, 1]
        if normalize and data.size:
            peak = max(abs(data.real).max(), abs(data.imag).max())
            if peak:
                data = data / peak
        return cls(data)

    def recipe(self, samples):
        if len(self._words) != samples:
            raise ValueError(f"Uploaded waveform must contain {samples} samples")
        return dict(type="file", samples=samples)
