#!/usr/bin/env python

"""Parse IQ-sampling chunk messages streamed by sensors in IQ mode."""

import zlib

import numpy as np

__all__ = [
    "IQ_CHUNK_SAMPLE_WORDS",
    "IQ_CHUNK_TYPE_HEADER",
    "IQ_ACCUM_TYPE_HEADER",
    "RX_GAIN_TABLE_ENTRIES",
    "IQAccumPacket",
    "IQChunkPacket",
]

# The receiver gain table has 77 real entries (indices 0..76). The firmware
# additionally uses slot 79 as a session-local shadow of the active forced
# entry: the DC-offset servo latches its updated DAC codes into the front end
# by selecting that bit-identical shadow for ~1 us (sensor-firmware
# modem_dcoc_write_entry). Samples captured during the latch pulse are taken
# at the unchanged analog gain of the active entry, but the hardware stamps
# the shadow's index into their gain field.
RX_GAIN_TABLE_ENTRIES = 77

IQ_CHUNK_TYPE_HEADER = 0x32435149  # "IQC2" little-endian (b"IQC2")
IQ_ACCUM_TYPE_HEADER = 0x31415149  # "IQA1" little-endian (b"IQA1")
IQ_CHUNK_SAMPLE_WORDS = 256
IQ_SYNC_INFO_SEQ_MASK = 0x0000FFFF
IQ_SYNC_INFO_GRID_SYNCED = 1 << 16
IQ_SYNC_INFO_BOOT_SYNCED = 1 << 17
IQ_SYNC_INFO_FIRE_TIME_VALID = 1 << 18
IQ_CHUNK_FLAG_SIGNAL_CAPTURE = 1 << 8
IQ_CHUNK_FLAG_SIGNAL_FIRST = 1 << 9
IQ_CHUNK_FLAG_SIGNAL_LAST = 1 << 10


class IQChunkPacket:
    """One IQ-sampling chunk streamed by a sensor in IQ mode (magic "IQC2")."""

    def __init__(self, pktbuf):
        if len(pktbuf) < 76 + 4 * IQ_CHUNK_SAMPLE_WORDS:
            raise ValueError("IQ chunk packet too short")
        self.type_header = int.from_bytes(pktbuf[0:4], byteorder="little")
        if self.type_header != IQ_CHUNK_TYPE_HEADER:
            raise ValueError("Unexpected IQ chunk type header")
        hdr = np.frombuffer(pktbuf[4:72], dtype="<u4")
        (
            self.sequence,
            self.source_chunk_index,
            self.chunk_counter,
            self.adc_decimation,
            self.sample_rate_hz,
            self.center_freq_hz,
            _rx_gain,
            _gain_mode,
            self.config_generation,
            self.flags,
            self.dropped_chunks,
            self.capture_id,
            self.capture_chunk_offset,
            self.producer_wake_write_ptr,
            fire_lo,
            fire_hi,
            self.sync_info,
        ) = (int(x) for x in hdr)
        # Signal mode reuses the old wake-pointer diagnostic word for the
        # event's explicit total length without changing the IQC2 wire size.
        self.capture_chunk_count = self.producer_wake_write_ptr if self.flags & IQ_CHUNK_FLAG_SIGNAL_CAPTURE else 0
        self.center_freq_mhz = self.center_freq_hz / 1e6
        self.fire_time_ns = (fire_hi << 32) | fire_lo
        self.samples = np.frombuffer(pktbuf[72 : 72 + 4 * IQ_CHUNK_SAMPLE_WORDS], dtype="<u4").copy()
        self.crc32 = int.from_bytes(pktbuf[72 + 4 * IQ_CHUNK_SAMPLE_WORDS : 76 + 4 * IQ_CHUNK_SAMPLE_WORDS], byteorder="little")
        # The SPI uplink between sensor and controller has no line-level CRC;
        # rare bit errors DO occur (measured: lone corrupted samples looking
        # like high-amplitude one-sample spikes). The chunk CRC is computed on
        # the sensor at emission, so verify it here and drop bad chunks, same
        # as the serialized-CSI path does.
        if zlib.crc32(bytes(pktbuf[: 72 + 4 * IQ_CHUNK_SAMPLE_WORDS])) != self.crc32:
            raise ValueError("IQ chunk CRC mismatch (uplink corruption)")

    @property
    def is_signal_capture(self) -> bool:
        return bool(self.flags & IQ_CHUNK_FLAG_SIGNAL_CAPTURE)

    @property
    def is_signal_first(self) -> bool:
        return bool(self.flags & IQ_CHUNK_FLAG_SIGNAL_FIRST)

    @property
    def is_signal_last(self) -> bool:
        return bool(self.flags & IQ_CHUNK_FLAG_SIGNAL_LAST)

    def decode_iq(self):
        """Complex baseband samples. Each 32-bit dump word packs I in bits 0-9 and
        Q in bits 10-19 (both 10-bit signed two's complement); bits 20-31 carry
        rx_gain / AGC state."""
        w = self.samples.astype(np.uint32)
        i = (w & 0x3FF).astype(np.int32)
        q = ((w >> 10) & 0x3FF).astype(np.int32)
        i = np.where(i & 0x200, i - 0x400, i)
        q = np.where(q & 0x200, q - 0x400, q)
        # This dump tap's Q polarity is opposite to the conventional complex
        # baseband orientation used by CSI: an RF tone above the LO otherwise
        # appears at a negative baseband frequency. Conjugate at the interface
        # boundary so CSI and IQ share one physical phase convention.
        return (i - 1j * q).astype(np.complex64)

    def sample_rx_gain(self):
        """Effective gain-table index stored alongside every ADC sample.

        Samples stamped with the DC-offset servo's shadow slot (see
        :data:`RX_GAIN_TABLE_ENTRIES`) were captured at the analog gain of the
        active forced entry, which every other sample of the chunk carries —
        so out-of-table indices are replaced by the chunk's prevailing valid
        index. Raw fields are available via :attr:`samples` (bits 20-27; bit
        27 marks the alternate gain-memory bank, both banks use the same
        seven-bit gain-table index)."""

        gains = ((self.samples >> 20) & 0x7F).astype(np.uint8)
        invalid = gains >= RX_GAIN_TABLE_ENTRIES
        if invalid.any():
            valid = gains[~invalid]
            if valid.size:
                gains[invalid] = np.bincount(valid).argmax()
        return gains


class IQAccumPacket:
    """One 256-sample section of an accumulated IQ vector (magic ``IQA1``).

    The wire payload keeps exact signed int32 I/Q sums. :meth:`decode_iq`
    returns their count-normalized complex average by default, while
    :attr:`i_sum` and :attr:`q_sum` expose the lossless integer components.
    """

    HEADER_BYTES = 88
    PAYLOAD_BYTES = 8 * IQ_CHUNK_SAMPLE_WORDS
    WIRE_BYTES = HEADER_BYTES + PAYLOAD_BYTES + 4

    def __init__(self, pktbuf):
        if len(pktbuf) < self.WIRE_BYTES:
            raise ValueError("IQ accumulation packet too short")
        self.type_header = int.from_bytes(pktbuf[0:4], byteorder="little")
        if self.type_header != IQ_ACCUM_TYPE_HEADER:
            raise ValueError("Unexpected IQ accumulation type header")
        hdr = np.frombuffer(pktbuf[4 : self.HEADER_BYTES], dtype="<u4")
        (
            self.sequence,
            self.window_index,
            self.source_chunk_start,
            self.source_chunk_end,
            self.vector_chunks,
            self.vector_chunk_index,
            self.nominal_chunks,
            self.accumulated_chunks,
            self.coverage_hash,
            self.adc_decimation,
            self.sample_rate_hz,
            self.center_freq_hz,
            self.rx_gain,
            self.gain_mode,
            self.config_generation,
            self.flags,
            self.dropped_windows,
            self.deadline_aborts,
            fire_lo,
            fire_hi,
            self.sync_info,
        ) = (int(x) for x in hdr)
        if not 1 <= self.vector_chunks <= 4:
            raise ValueError("Invalid accumulation vector length")
        if self.vector_chunk_index >= self.vector_chunks:
            raise ValueError("Invalid accumulation vector section index")
        window_chunks = (self.source_chunk_end - self.source_chunk_start) & 0xFFFFFFFF
        if window_chunks == 0 or window_chunks % self.vector_chunks:
            raise ValueError("Invalid accumulation source window")
        if not 1 <= self.nominal_chunks <= window_chunks // self.vector_chunks:
            raise ValueError("Invalid nominal accumulation count")
        if self.accumulated_chunks > self.nominal_chunks:
            raise ValueError("Actual accumulation count exceeds nominal count")
        self.center_freq_mhz = self.center_freq_hz / 1e6
        self.fire_time_ns = (fire_hi << 32) | fire_lo
        sums = (
            np.frombuffer(
                pktbuf[self.HEADER_BYTES : self.HEADER_BYTES + self.PAYLOAD_BYTES],
                dtype="<i4",
            )
            .reshape(IQ_CHUNK_SAMPLE_WORDS, 2)
            .copy()
        )
        self.i_sum = sums[:, 0]
        self.q_sum = sums[:, 1]
        crc_offset = self.HEADER_BYTES + self.PAYLOAD_BYTES
        self.crc32 = int.from_bytes(pktbuf[crc_offset : crc_offset + 4], byteorder="little")
        if zlib.crc32(bytes(pktbuf[:crc_offset])) != self.crc32:
            raise ValueError("IQ accumulation CRC mismatch (uplink corruption)")

    @property
    def is_partial(self) -> bool:
        return bool(self.flags & 1)

    @property
    def deadline_aborted(self) -> bool:
        return bool(self.flags & 2)

    def decode_iq(self, normalize=True):
        """Decode using the CSI-compatible ``I - jQ`` phase convention.

        With ``normalize=True`` (default), divide by the actual number of
        source chunks included in this vector section. A zero-count section
        decodes to complex NaNs. ``normalize=False`` returns complex128 sums;
        use :attr:`i_sum`/:attr:`q_sum` when exact integer values are needed.
        """

        iq = self.i_sum.astype(np.float64) - 1j * self.q_sum.astype(np.float64)
        if normalize:
            if self.accumulated_chunks == 0:
                iq[:] = np.nan + 1j * np.nan
            else:
                iq /= self.accumulated_chunks
        return iq.astype(np.complex64 if normalize else np.complex128)

    def sample_rx_gain(self):
        """Manual gain index repeated over the decoded vector section."""

        return np.full(IQ_CHUNK_SAMPLE_WORDS, self.rx_gain & 0x7F, dtype=np.uint8)
