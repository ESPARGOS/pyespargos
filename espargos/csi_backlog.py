#!/usr/bin/env python

"""Store selected values from completed CSI clusters.

The generic ring-buffer mechanics live in :mod:`espargos.sensor_backlog`.
This module describes the fields available for CSI and translates each
completed :class:`.CSICluster` into one backlog datapoint. Packet filtering,
CSI deserialization, calibration, and radar metadata remain CSI-specific.
"""

import logging
import re
import threading

import numpy as np

from . import csi_packet
from .sensor_backlog import BacklogField, SensorBacklog


class CSIBacklogFilter(object):
    """Base class for filters applied to completed CSI clusters."""

    def matches(self, clustered_csi):
        """Return whether a CSI cluster should enter the backlog."""

        raise NotImplementedError("CSIBacklogFilter subclasses must implement matches()")


class MacFilter(CSIBacklogFilter):
    """Match source MAC addresses against a regular expression."""

    def __init__(self, filter_regex):
        self.filter_regex = filter_regex
        self._compiled_regex = re.compile(filter_regex)

    def matches(self, clustered_csi):
        return self._compiled_regex.match(clustered_csi.get_source_mac()) is not None


class Exclude11bFilter(CSIBacklogFilter):
    """Drop 802.11b packets, which do not carry CSI."""

    def matches(self, clustered_csi):
        return not clustered_csi.is_11b()


class CSIBacklog(SensorBacklog):
    """Ring buffer containing selected fields from completed CSI clusters.

    :param pool: CSI pool from which clusters are collected
    :param fields: Fields to store, or ``None`` for all available fields
    :param calibrate: Whether to apply the pool's CSI calibration
    :param cb_predicate: CSI-cluster completion predicate
    :param size: Number of datapoints retained
    """

    FIELD_SPECS = {
        "lltf": BacklogField(
            (csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL,),
            np.complex64,
            per_sensor=True,
            fill_value=np.nan,
        ),
        "ht20": BacklogField(
            (csi_packet.HT_COEFFICIENTS_PER_CHANNEL,),
            np.complex64,
            per_sensor=True,
            fill_value=np.nan,
        ),
        "ht40": BacklogField(
            (csi_packet.HT_COEFFICIENTS_PER_CHANNEL + csi_packet.HT40_GAP_SUBCARRIERS + csi_packet.HT_COEFFICIENTS_PER_CHANNEL,),
            np.complex64,
            per_sensor=True,
            fill_value=np.nan,
        ),
        "he20": BacklogField(
            (csi_packet.HE20_COEFFICIENTS_PER_CHANNEL,),
            np.complex64,
            per_sensor=True,
            fill_value=np.nan,
        ),
        "rssi": BacklogField((), np.float32, per_sensor=True, fill_value=np.nan),
        "rx_gain": BacklogField((), np.float32, per_sensor=True, fill_value=np.nan),
        "fft_gain": BacklogField((), np.float32, per_sensor=True, fill_value=np.nan),
        "cfo": BacklogField((), np.float32, per_sensor=True, fill_value=np.nan),
        "lltf_8bit_mode": BacklogField(
            (),
            np.bool_,
            per_sensor=True,
            fill_value=False,
        ),
        "rfswitch_state": BacklogField(
            (),
            np.uint8,
            per_sensor=True,
            fill_value=0,
        ),
        "timestamp": BacklogField(
            (),
            np.float64,
            per_sensor=True,
            fill_value=np.nan,
        ),
        "host_timestamp": BacklogField(
            (),
            np.float64,
            per_sensor=False,
            fill_value=np.nan,
        ),
        "mac": BacklogField(
            (6,),
            np.uint8,
            per_sensor=False,
            fill_value=0,
        ),
        "radar_tx_timestamp": BacklogField(
            (),
            np.float64,
            per_sensor=False,
            fill_value=np.nan,
        ),
        "radar_tx_index": BacklogField(
            (),
            np.int16,
            per_sensor=False,
            fill_value=-1,
        ),
        "radar_tx_power": BacklogField(
            (),
            np.int16,
            per_sensor=False,
            fill_value=-1,
        ),
        "radar_tx_rfswitch_state": BacklogField(
            (),
            np.uint8,
            per_sensor=False,
            fill_value=0,
        ),
    }

    # Retain the established metadata view for callers that enumerate fields.
    DATA_FORMATS = {
        name: {
            "shape": spec.shape,
            "per_antenna": spec.per_sensor,
            "dtype": spec.dtype,
        }
        for name, spec in FIELD_SPECS.items()
    }

    def __init__(
        self,
        pool,
        fields=None,
        calibrate=True,
        cb_predicate=None,
        size=100,
    ):
        self.pool = pool
        self.calibrate = calibrate
        self.filter_mutex = threading.Lock()
        self.filters = []
        self.running = False
        self.thread = None
        self._callback_handle = None

        super().__init__(
            sensor_shape=pool.get_shape(),
            field_specs=self.FIELD_SPECS,
            fields=fields,
            size=size,
            logger=logging.getLogger("pyespargos.csi_backlog"),
        )
        self.set_callback_predicate(cb_predicate)

    def set_callback_predicate(self, cb_predicate=None) -> None:
        """Replace the CSI completion predicate used by this backlog."""

        if self._callback_handle is None:
            self._callback_handle = self.pool.add_csi_callback(
                self._on_new_csi,
                cb_predicate=cb_predicate,
            )
        else:
            self._callback_handle = self.pool.replace_csi_callback(
                self._callback_handle,
                self._on_new_csi,
                cb_predicate=cb_predicate,
            )

    def _deserialize_csi_field(
        self,
        clustered_csi,
        field,
        enabled_fields,
        available_method,
        deserialize_method,
        calibration_method,
        values,
    ) -> None:
        if field not in enabled_fields:
            return
        if not getattr(clustered_csi, available_method)():
            self.logger.debug(
                "Received a CSI cluster without %s even though that backlog field is enabled",
                field,
            )
            return

        csi = getattr(clustered_csi, deserialize_method)()
        if self.calibrate:
            calibration = self.pool.get_calibration()
            if calibration is None:
                raise RuntimeError("CSI calibration is enabled but the pool has no calibration")
            csi = getattr(calibration, calibration_method)(csi)
        values[field] = csi

    def _on_new_csi(self, clustered_csi):
        """Translate one accepted CSI cluster into a backlog datapoint."""

        with self.filter_mutex:
            filters = tuple(self.filters)
        if any(not backlog_filter.matches(clustered_csi) for backlog_filter in filters):
            return

        fields = self.get_fields()
        values = {}

        if "timestamp" in fields:
            values["timestamp"] = clustered_csi.get_sensor_timestamps()
        if "host_timestamp" in fields:
            values["host_timestamp"] = clustered_csi.get_host_timestamp()

        self._deserialize_csi_field(
            clustered_csi,
            "lltf",
            fields,
            "has_lltf",
            "deserialize_csi_lltf",
            "apply_lltf",
            values,
        )
        self._deserialize_csi_field(
            clustered_csi,
            "ht20",
            fields,
            "has_ht20ltf",
            "deserialize_csi_ht20ltf",
            "apply_ht20",
            values,
        )
        self._deserialize_csi_field(
            clustered_csi,
            "ht40",
            fields,
            "has_ht40ltf",
            "deserialize_csi_ht40ltf",
            "apply_ht40",
            values,
        )
        self._deserialize_csi_field(
            clustered_csi,
            "he20",
            fields,
            "has_he20ltf",
            "deserialize_csi_he20ltf",
            "apply_he20",
            values,
        )

        metadata_getters = {
            "rssi": "get_rssi",
            "rx_gain": "get_rx_gain",
            "fft_gain": "get_fft_gain",
            "cfo": "get_cfo",
            "lltf_8bit_mode": "get_lltf_8bit_mode",
            "rfswitch_state": "get_rfswitch_state",
        }
        for field, getter_name in metadata_getters.items():
            if field in fields:
                values[field] = getattr(clustered_csi, getter_name)()

        if "mac" in fields:
            mac_string = clustered_csi.get_source_mac()
            mac = np.asarray(
                [int(mac_string[index : index + 2], 16) for index in range(0, len(mac_string), 2)],
                dtype=np.uint8,
            )
            if mac.shape != (6,):
                raise ValueError(f"Unexpected CSI source MAC {mac_string!r}")
            values["mac"] = mac

        radar_fields = {
            "radar_tx_timestamp",
            "radar_tx_index",
            "radar_tx_power",
            "radar_tx_rfswitch_state",
        }
        if fields & radar_fields and clustered_csi.has_radar_tx_report():
            radar_tx_report = clustered_csi.get_radar_tx_info()
            if "radar_tx_timestamp" in fields:
                values["radar_tx_timestamp"] = radar_tx_report.get_hardware_tx_timestamp_ns() / 1e9
            if "radar_tx_index" in fields:
                values["radar_tx_index"] = clustered_csi.get_radar_tx_index()
            if "radar_tx_power" in fields:
                values["radar_tx_power"] = radar_tx_report.tx_power
            if "radar_tx_rfswitch_state" in fields:
                values["radar_tx_rfswitch_state"] = radar_tx_report.rfswitch_state

        self.append_datapoint(values)

    def start(self):
        """Start a worker that regularly processes the CSI pool."""

        if self.thread is not None and self.thread.is_alive():
            return
        self.running = True
        self.thread = threading.Thread(
            target=self._run,
            name="csi-backlog",
            daemon=True,
        )
        self.thread.start()
        self.logger.info("Started CSI backlog thread")

    def stop(self):
        """Stop the pool-processing worker, if running."""

        self.running = False
        if self.thread is not None:
            self.thread.join()
            self.thread = None

    def close(self):
        """Stop processing and detach this backlog from its CSI pool."""

        self.stop()
        if self._callback_handle is not None:
            self.pool.remove_csi_callback(self._callback_handle)
            self._callback_handle = None

    def add_filter(self, backlog_filter):
        """Add a CSI-cluster filter."""

        if not isinstance(backlog_filter, CSIBacklogFilter):
            raise TypeError("backlog_filter must be an instance of CSIBacklogFilter")
        with self.filter_mutex:
            if backlog_filter not in self.filters:
                self.filters.append(backlog_filter)

    def remove_filter(self, backlog_filter):
        """Remove a CSI-cluster filter."""

        with self.filter_mutex:
            if backlog_filter in self.filters:
                self.filters.remove(backlog_filter)

    def clear_filters(self):
        """Remove all CSI-cluster filters."""

        with self.filter_mutex:
            self.filters.clear()

    def get_filters(self):
        """Return the currently active CSI-cluster filters."""

        with self.filter_mutex:
            return list(self.filters)

    def _run(self):
        while self.running:
            self.pool.run(timeout=0.5)
