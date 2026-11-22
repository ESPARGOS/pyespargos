"""Continuous narrowband IQ from one sensor or a synchronized array."""

import struct
import zlib

import numpy as np

from .iq_packet import IQ_SYNC_INFO_GRID_SYNCED
from .sensor_cluster import ClusterCollisionError, SensorCluster

IQ_RX_STREAM_TYPE_HEADER = int.from_bytes(b"IRS1", "little")


class IQStreamPacket:
    """One sensor's consecutive complex samples, indexed on the output grid."""

    _HEADER = struct.Struct("<4IQ2I")
    SAMPLE_COUNT = 128

    def __init__(self, data):
        if len(data) != self._HEADER.size + self.SAMPLE_COUNT * 4 + 4:
            raise ValueError("Invalid IQ stream block length")
        magic, self.config_generation, self.sync_info, self.center_freq_hz, self.first_sample, self.sample_rate_hz, _ = self._HEADER.unpack_from(data)
        if magic != IQ_RX_STREAM_TYPE_HEADER or not self.sample_rate_hz:
            raise ValueError("Invalid IQ stream header")
        if zlib.crc32(memoryview(data)[:-4]) != struct.unpack_from("<I", data, len(data) - 4)[0]:
            raise ValueError("Invalid IQ stream checksum")
        self.samples = np.frombuffer(data, dtype="<i2", offset=self._HEADER.size, count=self.SAMPLE_COUNT * 2).reshape(-1, 2)

    @property
    def synchronized(self):
        return bool(self.sync_info & IQ_SYNC_INFO_GRID_SYNCED)

    def decode_iq(self):
        """Return complex64 IQ in ADC counts, with the same polarity as raw IQ."""
        return self.samples.astype(np.float32).view(np.complex64).reshape(-1) / 32


class IQStreamBlock(SensorCluster):
    """Time-aligned samples with shape (board, row, column, sample).

    Missing antennas have NaN samples and a false entry in :attr:`completion`.
    A jump in :attr:`first_sample` exposes missing whole blocks.
    """

    def __init__(self, packet, revisions):
        super().__init__(revisions)
        self.first_sample = packet.first_sample
        self.sample_rate_hz = packet.sample_rate_hz
        self.center_freq_hz = packet.center_freq_hz
        self.synchronized = packet.synchronized
        self.settled = False
        self.iq = np.full((*self.shape, packet.SAMPLE_COUNT), np.nan, dtype=np.complex64)
        self.config_generation = np.zeros(self.shape, dtype=np.uint32)

    def add_message(self, board_index, message):
        packet = message.payload
        position = self.get_sensor_position(board_index, message.antenna_id)
        x = packet.decode_iq()
        if self.completion[position]:
            if np.array_equal(self.iq[position], x):
                return False
            raise ClusterCollisionError(f"Conflicting IQ samples at {self.first_sample} for {position}")
        self.iq[position] = x
        self.config_generation[position] = packet.config_generation
        self._mark_sensor_position_complete(position)
        return True
