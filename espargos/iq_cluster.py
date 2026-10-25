#!/usr/bin/env python

"""Assemble IQ chunks with the same synchronized index into one array snapshot.

After broadband reference sync (:meth:`espargos.IQReceiver.sync`), ordinary
raw-IQ chunks use their common ``source_chunk_index`` as the association key.
Signal chunks use ``(config_generation, capture_id, capture_chunk_offset)``
because that tuple identifies an event within one array. Separate arrays
trigger independently, so these event IDs cannot be used to match captures
across arrays. An :class:`IQCluster`
collects one such association key across the configured sensors.
"""

import time

import numpy as np

from espargos import gain_phase_calibration
from espargos import sensor
from espargos.sensor_cluster import ClusterCollisionError, SensorCluster

from .iq_packet import (
    IQ_CHUNK_SAMPLE_WORDS as CHUNK_SAMPLES,
    RX_GAIN_TABLE_ENTRIES,
)

__all__ = ["CHUNK_SAMPLES", "IQCluster"]


class IQCluster(SensorCluster):
    """The IQ chunks of one synchronized chunk index, across all boards.

    Sample data is stored at the sensors' logical ``(board, row, column)``
    positions. Positions whose chunk did not arrive (SPI uplink drops) stay
    NaN; :attr:`completion` reports which positions supplied data.
    """

    def __init__(
        self,
        chunk_index: int,
        board_revisions,
        gain_phase_compensation: bool = False,
    ):
        super().__init__(board_revisions)
        self.chunk_index = int(chunk_index)
        self._gain_phase_enabled = bool(gain_phase_compensation)
        #: Set by the pool once the cluster's settle window has elapsed: no
        #: more chunks are expected (missing positions are genuine uplink
        #: drops). Callback predicates use this to accept partial snapshots.
        self.settled = False
        self._host_timestamp = time.time()
        self._iq = np.full(self.shape + (CHUNK_SAMPLES,), np.nan, dtype=np.complex64)
        self._adc_peak = np.full(self.shape, np.nan, dtype=np.float32)
        self._sample_rx_gain = np.full(self.shape + (CHUNK_SAMPLES,), 0xFF, dtype=np.uint8)
        self._flags = np.zeros(self.shape, dtype=np.uint32)
        self._sample_rate_hz = np.zeros(self.shape, dtype=np.uint32)
        self._center_freq_hz = np.zeros(self.shape, dtype=np.uint32)
        self._config_generation = np.zeros(self.shape, dtype=np.uint32)
        self._sync_info = np.zeros(self.shape, dtype=np.uint32)
        self._fire_time_ns = np.zeros(self.shape, dtype=np.uint64)
        self._dropped_chunks = np.zeros(self.shape, dtype=np.uint32)
        self._source_chunk_index = np.zeros(self.shape, dtype=np.uint32)
        self._chunk_counter = np.zeros(self.shape, dtype=np.uint32)
        self._capture_id = np.full(self.shape, 0xFFFFFFFF, dtype=np.uint32)
        self._capture_chunk_offset = np.full(self.shape, 0xFFFFFFFF, dtype=np.uint32)
        self._capture_chunk_count = np.zeros(self.shape, dtype=np.uint32)

    def add_message(self, board_index: int, sensor_message: sensor.SensorMessage) -> bool:
        position = self.get_sensor_position(board_index, int(sensor_message.antenna_id))
        payload = sensor_message.payload
        iq = payload.decode_iq()
        adc_peak = max(np.max(np.abs(iq.real)), np.max(np.abs(iq.imag)))
        rx_gain = payload.sample_rx_gain()
        # A rare dump chunk can consist entirely of samples stamped with the
        # DCO servo's out-of-table shadow slot.  There is then no trustworthy
        # gain index from which to select a phase correction.  Keep the IQ
        # usable and preserve the invalid metadata for diagnostics; applying
        # no correction to one chunk is preferable to killing the pool's
        # processing thread (and phase-only compensation is irrelevant to
        # single-channel consumers such as the WiFi packet receiver).
        if self._gain_phase_enabled and np.all(rx_gain < RX_GAIN_TABLE_ENTRIES):
            iq = gain_phase_calibration.apply(iq, rx_gain)
        if self._completion[position]:
            if np.array_equal(self._iq[position], iq):
                return False
            raise ClusterCollisionError(f"different IQ data for chunk index {self.chunk_index} at sensor position {position}")
        self._iq[position] = iq
        self._adc_peak[position] = adc_peak
        self._sample_rx_gain[position] = rx_gain
        self._flags[position] = int(getattr(payload, "flags", 0))
        self._sample_rate_hz[position] = int(getattr(payload, "sample_rate_hz", 0))
        self._center_freq_hz[position] = int(getattr(payload, "center_freq_hz", 0))
        self._config_generation[position] = int(getattr(payload, "config_generation", 0))
        self._sync_info[position] = int(getattr(payload, "sync_info", 0))
        self._fire_time_ns[position] = int(getattr(payload, "fire_time_ns", 0))
        self._dropped_chunks[position] = int(getattr(payload, "dropped_chunks", 0))
        self._source_chunk_index[position] = int(getattr(payload, "source_chunk_index", 0))
        self._chunk_counter[position] = int(payload.chunk_counter)
        self._capture_id[position] = int(getattr(payload, "capture_id", 0xFFFFFFFF))
        self._capture_chunk_offset[position] = int(getattr(payload, "capture_chunk_offset", 0xFFFFFFFF))
        self._capture_chunk_count[position] = int(getattr(payload, "capture_chunk_count", 0))
        self._mark_sensor_position_complete(position)
        return True

    @property
    def iq(self) -> np.ndarray:
        """Return the ``(board, row, column, sample)`` snapshot (NaN where missing)."""

        return self._iq

    @property
    def adc_peak(self) -> np.ndarray:
        """Largest absolute raw I/Q component per sensor, before phase correction."""

        return self._adc_peak

    @property
    def grid_origin_ns(self) -> np.ndarray:
        """Local CPU time of grid sample zero, including measured fire latency.

        The 24-bit grid index is unambiguous within one wrap of a new sync.
        This permits comparing capture starts at different sample rates.
        """
        base = (self._source_chunk_index.astype(np.int64) - self._chunk_counter) & 0xFFFFFF
        offset_ns = np.divide(base * 256e9, self._sample_rate_hz, out=np.zeros(self.shape), where=self._sample_rate_hz != 0)
        return self._fire_time_ns.astype(np.int64) - np.rint(offset_ns).astype(np.int64)

    @property
    def host_timestamp(self) -> float:
        """Return the host wall-clock time the first chunk arrived at."""

        return self._host_timestamp

    @property
    def sample_rx_gain(self) -> np.ndarray:
        """Live gain-table index for every IQ sample."""

        return self._sample_rx_gain

    @property
    def flags(self) -> np.ndarray:
        """Per-sensor firmware flags for this chunk."""

        return self._flags

    @property
    def sample_rate_hz(self) -> np.ndarray:
        return self._sample_rate_hz

    @property
    def center_freq_hz(self) -> np.ndarray:
        return self._center_freq_hz

    @property
    def config_generation(self) -> np.ndarray:
        return self._config_generation

    @property
    def sync_info(self) -> np.ndarray:
        return self._sync_info

    @property
    def fire_time_ns(self) -> np.ndarray:
        return self._fire_time_ns

    @property
    def dropped_chunks(self) -> np.ndarray:
        """Sensor-side cumulative raw-IQ chunk loss counter."""

        return self._dropped_chunks

    @property
    def source_chunk_index(self) -> np.ndarray:
        """Per-sensor raw grid index (Signal timing/continuity diagnostics)."""

        return self._source_chunk_index

    @property
    def capture_id(self) -> np.ndarray:
        return self._capture_id

    @property
    def capture_chunk_offset(self) -> np.ndarray:
        return self._capture_chunk_offset

    @property
    def capture_chunk_count(self) -> np.ndarray:
        """Total chunks in the Signal event, repeated in every chunk."""

        return self._capture_chunk_count
