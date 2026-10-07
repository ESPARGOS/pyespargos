#!/usr/bin/env python

"""Assemble one array-wide accumulated IQ vector window."""

import time

import numpy as np

from espargos import gain_phase_calibration
from espargos.sensor_cluster import ClusterCollisionError, SensorCluster

from . import iq_packet
from .iq_packet import IQ_CHUNK_SAMPLE_WORDS

__all__ = ["IQAccumCluster"]


class IQAccumCluster(SensorCluster):
    """All vector sections and sensors for one accumulation window.

    :attr:`iq` has shape ``(boards, rows, columns, vector_chunks * 256)``.
    :attr:`counts` and :attr:`coverage_hashes` retain one value per sensor and
    vector section. A complete cluster is array-coherent only when
    :attr:`coverage_consistent` is true.
    """

    def __init__(self, first_packet, board_revisions, gain_phase_compensation=False):
        super().__init__(board_revisions)
        self.window_index = int(first_packet.window_index)
        self.source_chunk_start = int(first_packet.source_chunk_start)
        self.source_chunk_end = int(first_packet.source_chunk_end)
        self.vector_chunks = int(first_packet.vector_chunks)
        self.nominal_chunks = int(first_packet.nominal_chunks)
        self.config_generation = int(first_packet.config_generation)
        self.adc_decimation = int(first_packet.adc_decimation)
        self.sample_rate_hz = int(first_packet.sample_rate_hz)
        self.center_freq_hz = int(first_packet.center_freq_hz)
        self._gain_phase_enabled = bool(gain_phase_compensation)
        self.settled = False
        self._host_timestamp = time.time()
        section_shape = self.shape + (self.vector_chunks,)
        self._sections = np.full(section_shape + (IQ_CHUNK_SAMPLE_WORDS,), np.nan, dtype=np.complex64)
        self._counts = np.full(section_shape, -1, dtype=np.int32)
        self._coverage_hashes = np.zeros(section_shape, dtype=np.uint32)
        self._flags = np.zeros(section_shape, dtype=np.uint32)
        self._deadline_aborts = np.zeros(section_shape, dtype=np.uint32)
        self._fire_time_ns = np.zeros(self.shape, dtype=np.uint64)
        self._section_completion = np.zeros(section_shape, dtype=np.bool_)

    def add_message(self, board_index, sensor_message):
        payload = sensor_message.payload
        if (
            int(payload.window_index) != self.window_index
            or int(payload.source_chunk_start) != self.source_chunk_start
            or int(payload.source_chunk_end) != self.source_chunk_end
            or int(payload.vector_chunks) != self.vector_chunks
            or int(payload.nominal_chunks) != self.nominal_chunks
            or int(payload.config_generation) != self.config_generation
            or int(payload.adc_decimation) != self.adc_decimation
            or int(payload.sample_rate_hz) != self.sample_rate_hz
            or int(payload.center_freq_hz) != self.center_freq_hz
        ):
            raise ClusterCollisionError("inconsistent IQ accumulation window metadata")
        lane = int(payload.vector_chunk_index)
        position = self.get_sensor_position(board_index, int(sensor_message.antenna_id))
        slot = position + (lane,)
        iq = payload.decode_iq()
        if self._gain_phase_enabled:
            rx_gain = payload.sample_rx_gain()
            # The scalar wire gain is sampled from the live AGC register and
            # can transiently read the DC-offset servo's shadow slot (see
            # iq_packet.RX_GAIN_TABLE_ENTRIES). The forced entry it aliases is
            # not recoverable here, so leave such a window uncorrected rather
            # than failing the whole processing thread.
            if np.all(rx_gain < iq_packet.RX_GAIN_TABLE_ENTRIES):
                iq = gain_phase_calibration.apply(iq, rx_gain)
        if self._section_completion[slot]:
            if (
                np.array_equal(self._sections[slot], iq, equal_nan=True)
                and self._counts[slot] == int(payload.accumulated_chunks)
                and self._coverage_hashes[slot] == int(payload.coverage_hash)
                and self._deadline_aborts[slot] == int(payload.deadline_aborts)
            ):
                return False
            raise ClusterCollisionError("different IQ accumulation data for an existing section")
        self._sections[slot] = iq
        self._counts[slot] = int(payload.accumulated_chunks)
        self._coverage_hashes[slot] = int(payload.coverage_hash)
        self._flags[slot] = int(payload.flags)
        self._deadline_aborts[slot] = int(payload.deadline_aborts)
        self._fire_time_ns[position] = int(payload.fire_time_ns)
        self._section_completion[slot] = True
        if np.all(self._section_completion[position]):
            self._mark_sensor_position_complete(position)
        return True

    @property
    def iq_sections(self):
        """Normalized IQ with shape ``sensor_shape + (vector_chunks, 256)``."""

        return self._sections

    @property
    def iq(self):
        """Normalized, concatenated accumulation vector for each sensor."""

        return self._sections.reshape(self.shape + (self.vector_chunks * IQ_CHUNK_SAMPLE_WORDS,))

    @property
    def counts(self):
        return self._counts

    @property
    def coverage_hashes(self):
        return self._coverage_hashes

    @property
    def flags(self):
        return self._flags

    @property
    def deadline_aborts(self):
        """Cumulative sensor-side source-stage misses at this section."""

        return self._deadline_aborts

    @property
    def fire_time_ns(self) -> np.ndarray:
        return self._fire_time_ns

    @property
    def section_completion(self):
        return self._section_completion

    @property
    def coverage_consistent(self):
        """Whether every sensor accumulated the same source chunks per lane."""

        if not self.is_complete:
            return False
        flat_counts = self._counts.reshape((-1, self.vector_chunks))
        flat_hashes = self._coverage_hashes.reshape((-1, self.vector_chunks))
        return bool(np.all(flat_counts == flat_counts[0:1]) and np.all(flat_hashes == flat_hashes[0:1]))

    @property
    def host_timestamp(self):
        return self._host_timestamp
