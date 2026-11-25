"""WiFi packet captures at 20 MSa/s and legacy OFDM training estimates."""

import struct
from functools import cached_property

import numpy as np

from ..csi_processing import interpolate_lltf_gap
from ..packet_iq import complex_samples, unpack_words

# The two repeated L-LTF symbols occupy tones -26 through +26.
# fmt: off
_LTF = np.array([
     1,  1, -1, -1,  1,  1, -1,  1, -1,  1,  1,  1,  1,
     1,  1, -1, -1,  1,  1, -1,  1, -1,  1,  1,  1,  1,
     0,
     1, -1, -1,  1,  1, -1,  1, -1,  1, -1, -1, -1, -1,
    -1,  1,  1, -1, -1,  1, -1,  1, -1,  1,  1,  1,  1,
])
# fmt: on
RECORD_TYPES = (b"WFQ1", b"WFN1", b"WFN2", b"WFC1", b"WFC2", b"WFM1")


class WiFiIQPacket:
    """One sensor report. Metadata-only records have an empty sample array."""

    def __init__(self, data):
        self.kind = data[:4]
        native = self.kind in (b"WFN1", b"WFN2")
        header = 160 if native else 136
        if self.kind not in RECORD_TYPES or len(data) < header:
            raise ValueError("Invalid WiFi IQ record header")
        h = struct.unpack_from("<18I", data)
        self.sequence = h[1]
        self.boot_us, self.local_end_us = struct.unpack_from("<QQ", data, 8)
        self.time_us = h[6] | h[7] << 32
        self.clock_epoch, self.receiver_epoch, self.settings, self.flags = h[8:12]
        # Index of raw[0] in the sensor's free-running 16K-sample ADC ring.
        self.ring_start = (self.flags >> 16) & 0x3FFF if self.flags & (1 << 8) else None
        self.generation = h[16]
        words = h[14]
        packed = self.flags & 4
        metadata = self.kind == b"WFM1"
        if (words != 0 if metadata else not 128 <= words <= 16384) or words % 8 or h[15] != words:
            raise ValueError("Invalid WiFi IQ capture size")
        if len(data) != header + words * (5 if packed else 8) // 2:
            raise ValueError("WiFi IQ payload size mismatch")
        self.sample_rate, self.frequency_hz, self.evidence = struct.unpack_from("<III", data, 72)
        self.training, self.training_words, self.frame, self.end, self.length, self.sequence_control = struct.unpack_from("<6H", data, 84)
        self.phy, self.half_rate, self.rf_switch, self.gain, self.cfo = struct.unpack_from("<4Bi", data, 96)
        if self.sample_rate != 20_000_000 or data[110] > 24 or self.training_words and self.training + self.training_words > words:
            raise ValueError("Invalid WiFi IQ training or packet metadata")
        self.source_mac = data[104:110].hex() if self.evidence & 2 else None
        self.prefix = data[112 : 112 + data[110]]
        self.crc_valid = self.evidence & 12 == 12
        self.invalid_chunks = 0
        if native or self.kind.startswith(b"WFC"):
            self.invalid_chunks = int.from_bytes(data[header - 24 : header - 16], "little")
            self._gain_trace = data[header - 16 : header]
        self.raw = unpack_words(data, header, packed)

    @cached_property
    def samples(self):
        """Complex baseband samples in ADC counts, with the WiFi tap's Q polarity corrected."""
        return complex_samples(self.raw, conjugate=True)

    def _training_gain(self, start, end):
        if self.kind == b"WFQ1":
            within_training = self.training <= start and end <= self.training + self.training_words
            if self.evidence & 128 and self.gain < 77 and (within_training or (start >= self.training and self.evidence & (1 << 20))):
                return self.gain
            return None
        if self.kind.endswith(b"2"):
            bits = int.from_bytes(self._gain_trace, "little")
            gain = bits & 127
            previous = 0
            for n in range(6):
                entry = bits >> (7 + 20 * n) & 0xFFFFF
                index = entry & 8191
                if not entry:
                    break
                if index <= previous or index >= len(self.raw) or start < index < end:
                    return None
                if index <= start:
                    gain = entry >> 13
                previous = index
            return gain if gain < 77 else None
        # First-generation gain traces report one gain per block.
        size = (len(self.raw) + 15) // 16
        gains = set(self._gain_trace[start // size : (end - 1) // size + 1])
        return gains.pop() if len(gains) == 1 and max(gains) < 77 else None

    def valid_samples(self, start, end):
        """Return an intact interval of unsaturated ADC samples, if available."""
        if start < 0 or end > self.raw.size or end <= start:
            return None
        chunk = 64 if self.raw.size > 2048 else 32
        if any(self.invalid_chunks & (1 << n) for n in range(start // chunk, (end - 1) // chunk + 1)):
            return None
        x = self.samples[start:end]
        return None if np.any(np.abs(x.real) >= 500) or np.any(np.abs(x.imag) >= 500) else x

    @cached_property
    def phy_header(self):
        """CRC-validated HT-SIG or HE-SIG-A from this capture, if present."""
        from ..packet_decoders import wifi

        if self.phy == 1 or self.raw.size < 384:
            return None
        # A ring capture can include a previous packet. A native timestamp
        # identifies which preamble belongs to this record.
        anchored = self.training != 65535 and (self.training_words or self.evidence & 1)
        begin = max(0, self.training - 96) if anchored else 0
        end = self.training + 512 if anchored else self.raw.size
        h = wifi(self.raw[begin:end], stage="extended_header")
        if h is None or (h["phy"], len(h["prefix"])) not in ((2, 6), (4, 7)):
            return None
        start = h["training_start"] + begin
        h = dict(h, training_start=start)
        end = start + (368 if h["phy"] == 2 else 448)
        if self.valid_samples(start, end) is None:
            return None
        return h

    @cached_property
    def spatial_csi(self):
        """HT/HE training channels at this antenna, indexed by space-time stream and tone."""
        from .spatial import measure

        return measure(self)

    @cached_property
    def barker(self):
        if self.phy == 1 and self.raw.size:
            from .barker import barker_training

            return barker_training(self)

    @property
    def training_measurement(self):
        if self.phy == 1:
            b = self.barker
            return (b.start, b.cfo, b.gain, b.channel) if b is not None else None
        return self.ofdm

    @cached_property
    def ofdm(self):
        """Return (training offset, CFO, stable gain, L-LTF CSI), or None."""
        if self.phy == 1 or not self.raw.size:
            return None
        start, cfo = self.training, self.cfo
        if not self.training_words:
            from ..packet_decoders import wifi

            native = self.kind.startswith(b"WFN") or self.evidence & 1
            raw = self.raw[: self.frame] if native else self.raw
            if len(raw) < 128:
                return None
            result = self.phy_header if not native else None
            if result is None:
                result = wifi(
                    raw.astype("=u4", copy=False).tobytes(),
                    stage="native_ofdm" if native else "training",
                    training_hint=start if native and start != 65535 else 0,
                )
            if result is None:
                return None
            start, cfo = result["training_start"], result["cfo_hz"]
        elif self.training_words < 128:
            return None
        end = start + 128
        x = self.valid_samples(start, end)
        if x is None:
            return None
        # The L-LTF leaves DC empty. Remove the ADC offset before correcting
        # carrier frequency, or that offset spreads into the measured tones.
        x = (x - x.mean()) * np.exp(-2j * np.pi * cfo / self.sample_rate * np.arange(128))
        spectrum = np.fft.fftshift(np.fft.fft(x.reshape(2, 64).mean(axis=0)))[6:59] * _LTF
        interpolate_lltf_gap(spectrum)
        return start, cfo, self._training_gain(start, end), spectrum.astype(np.complex64)
