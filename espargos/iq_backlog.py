#!/usr/bin/env python

"""Store synchronized IQ chunk clusters in a ring buffer.

The generic ring-buffer and filter-chain mechanics live in
:mod:`espargos.sensor_backlog`; this module describes the fields available for
IQ snapshots and translates each delivered :class:`.IQCluster` into one
backlog datapoint — the IQ counterpart of :class:`espargos.CSIBacklog`.

The raw time-domain samples are stored uncalibrated: the pool's fine
calibration is a per-frequency correction
(:meth:`espargos.iq_pool.IQPool.cal_correction`), which consumers
apply in whatever spectral domain they process the samples in.
"""

import logging

import numpy as np

from espargos.sensor_backlog import BacklogField, BacklogFilter, SensorBacklog

from .iq_cluster import CHUNK_SAMPLES

__all__ = ["IQBacklog", "IQBacklogFilter"]


class IQBacklogFilter(BacklogFilter):
    """Base class for filters applied to delivered IQ clusters."""

    def matches(self, iq_cluster):
        """Return whether an IQ cluster should enter the backlog."""

        raise NotImplementedError("IQBacklogFilter subclasses must implement matches()")


class IQBacklog(SensorBacklog):
    """Ring buffer containing selected fields from delivered IQ clusters.

    :param pool: IQ pool from which clusters are collected
    :param fields: Fields to store, or ``None`` for all available fields
    :param callback_predicate: IQ-cluster completion predicate
    :param include_partial: Also store settled incomplete clusters (missing
        sensors stay NaN); see :meth:`espargos.iq_pool.IQPool.add_iq_callback`
    :param size: Number of datapoints retained
    """

    FIELD_SPECS = {
        "iq": BacklogField(
            (CHUNK_SAMPLES,),
            np.complex64,
            per_sensor=True,
            fill_value=np.nan,
        ),
        "chunk_index": BacklogField((), np.int64, per_sensor=False, fill_value=-1),
        "host_timestamp": BacklogField((), np.float64, per_sensor=False, fill_value=np.nan),
        "sample_rx_gain": BacklogField((CHUNK_SAMPLES,), np.uint8, per_sensor=True, fill_value=0xFF),
    }

    def __init__(
        self,
        pool,
        fields=None,
        callback_predicate=None,
        include_partial=False,
        size=100,
    ):
        self._pool = pool
        super().__init__(
            sensor_shape=pool.shape,
            field_specs=self.FIELD_SPECS,
            fields=fields,
            size=size,
            logger=logging.getLogger("pyespargos.iq_backlog"),
        )
        self._callback_handle = pool.add_iq_callback(
            self._on_new_cluster,
            callback_predicate=callback_predicate,
            include_partial=include_partial,
        )

    def _on_new_cluster(self, iq_cluster):
        """Translate one delivered IQ cluster into a backlog datapoint."""

        if not self._passes_filters(iq_cluster):
            return

        fields = self.fields
        values = {}
        if "iq" in fields:
            values["iq"] = iq_cluster.iq
        if "chunk_index" in fields:
            values["chunk_index"] = iq_cluster.chunk_index
        if "host_timestamp" in fields:
            values["host_timestamp"] = iq_cluster.host_timestamp
        if "sample_rx_gain" in fields:
            values["sample_rx_gain"] = iq_cluster.sample_rx_gain
        self.append_datapoint(values)

    def start(self):
        """Start the pool's processing worker so clusters reach this backlog."""

        self._pool.start_processing()

    def stop(self):
        """Stop the pool-processing worker, if running."""

        self._pool.stop_processing()

    def close(self):
        """Stop processing and detach this backlog from its IQ pool."""

        self.stop()
        if self._callback_handle is not None:
            self._pool.remove_iq_callback(self._callback_handle)
            self._callback_handle = None
