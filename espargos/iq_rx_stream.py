"""Continuous narrowband IQ from one sensor or a synchronized array."""

import struct
import zlib

import numpy as np

from .iq_packet import DECIM_TO_FS, IQ_SYNC_INFO_GRID_SYNCED, IQ_SYNC_INFO_FIRE_TIME_VALID
from .sensor_cluster import ClusterCollisionError, SensorCluster

IQ_RX_STREAM_RATES = (16000, 32000, 64000, 80000, 100000)
IQ_RX_STREAM_TYPE_HEADER = int.from_bytes(b"IRS1", "little")


def receive_sample_rate(config):
    trigger = config.get("trigger", {})
    if trigger.get("mode") == "stream":
        return trigger.get("sample_rate_hz", 16000)
    return DECIM_TO_FS[config["adc_decimation"]]


def gain_changed(previous, update):
    """Compare the gain settings that actually drive the narrowband receiver."""
    mode = update.get("gain_mode", previous.get("gain_mode"))
    if mode != previous.get("gain_mode"):
        return True
    key = {"manual": "rx_gain", "expert": "expert_gain_words"}.get(mode)
    return key in update and update[key] != previous.get(key)


class IQStreamPacket:
    """One sensor's consecutive complex samples, indexed on the output grid."""

    _HEADER = struct.Struct("<4I3QI")
    SAMPLE_COUNT = 128

    def __init__(self, data):
        if len(data) != self._HEADER.size + self.SAMPLE_COUNT * 4 + 4:
            raise ValueError("Invalid IQ stream block length")
        magic, self.config_generation, self.sync_info, self.center_freq_hz, self.first_sample, self.fire_time_ns, self.grid_origin_ns, self.sample_rate_hz = self._HEADER.unpack_from(data)
        if magic != IQ_RX_STREAM_TYPE_HEADER or self.sample_rate_hz not in IQ_RX_STREAM_RATES:
            raise ValueError("Invalid IQ stream header")
        if zlib.crc32(memoryview(data)[:-4]) != struct.unpack_from("<I", data, len(data) - 4)[0]:
            raise ValueError("Invalid IQ stream checksum")
        self.samples = np.frombuffer(data, dtype="<i2", offset=self._HEADER.size, count=self.SAMPLE_COUNT * 2).reshape(-1, 2)

    @property
    def synchronized(self):
        flags = IQ_SYNC_INFO_GRID_SYNCED | IQ_SYNC_INFO_FIRE_TIME_VALID
        return self.sync_info & flags == flags

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
        self.sync_info = packet.sync_info
        self.first_sample = packet.first_sample
        self.sample_rate_hz = packet.sample_rate_hz
        self.center_freq_hz = packet.center_freq_hz
        self.synchronized = packet.synchronized
        self.settled = False
        self.iq = np.full((*self.shape, packet.SAMPLE_COUNT), np.nan, dtype=np.complex64)
        self.fire_time_ns = np.zeros(self.shape, dtype=np.uint64)
        self.grid_origin_ns = np.zeros(self.shape, dtype=np.uint64)
        self.config_generation = np.zeros(self.shape, dtype=np.uint32)

    def add_message(self, board_index, message):
        packet = message.payload
        position = self.get_sensor_position(board_index, message.antenna_id)
        x = packet.decode_iq()
        if self.completion[position]:
            if np.array_equal(self.iq[position], x):
                return False
            raise ClusterCollisionError(f"Conflicting IQ samples at {self.first_sample} for {position}")
        self.fire_time_ns[position] = packet.fire_time_ns
        self.grid_origin_ns[position] = packet.grid_origin_ns
        self.iq[position] = x
        self.config_generation[position] = packet.config_generation
        self._mark_sensor_position_complete(position)
        return True
