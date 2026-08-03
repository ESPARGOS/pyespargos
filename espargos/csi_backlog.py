#!/usr/bin/env python

"""Store selected values from completed CSI clusters.

The generic ring-buffer and filter-chain mechanics live in
:mod:`espargos.sensor_backlog`. This module describes the fields available for
CSI and translates each completed :class:`.CSICluster` into one backlog
datapoint. CSI deserialization, calibration, radar metadata, and the concrete
packet filters remain CSI-specific.
"""

import logging
import re

import numpy as np

from . import csi_packet
from .sensor_backlog import BacklogField, BacklogFilter, SensorBacklog

__all__ = ["CSIBacklog", "CSIBacklogFilter", "Exclude11bFilter", "MACFilter"]


class CSIBacklogFilter(BacklogFilter):
    """Base class for filters applied to completed CSI clusters."""

    def matches(self, clustered_csi):
        """Return whether a CSI cluster should enter the backlog."""

        raise NotImplementedError("CSIBacklogFilter subclasses must implement matches()")


class MACFilter(CSIBacklogFilter):
    """Match source MAC addresses against a regular expression."""

    def __init__(self, filter_regex):
        self._pattern = filter_regex
        self._compiled_pattern = re.compile(filter_regex)

    def matches(self, clustered_csi):
        return self._compiled_pattern.match(clustered_csi.source_mac) is not None


class Exclude11bFilter(CSIBacklogFilter):
    """Drop 802.11b packets, which do not carry CSI."""

    def matches(self, clustered_csi):
        return not clustered_csi.is_11b


class CSIBacklog(SensorBacklog):
    """Ring buffer containing selected fields from completed CSI clusters.

    :param pool: CSI pool from which clusters are collected
    :param fields: Fields to store, or ``None`` for all available fields
    :param apply_calibration: Whether to apply the pool's CSI calibration
    :param callback_predicate: CSI-cluster completion predicate
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
        "rf_switch_state": BacklogField(
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
        "radar_tx_rf_switch_state": BacklogField(
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
            "per_sensor": spec.per_sensor,
            "dtype": spec.dtype,
        }
        for name, spec in FIELD_SPECS.items()
    }

    def __init__(
        self,
        pool,
        fields=None,
        apply_calibration=True,
        callback_predicate=None,
        size=100,
    ):
        self._pool = pool
        self._apply_calibration = bool(apply_calibration)
        self._callback_handle = None

        super().__init__(
            sensor_shape=pool.shape,
            field_specs=self.FIELD_SPECS,
            fields=fields,
            size=size,
            logger=logging.getLogger("pyespargos.csi_backlog"),
        )
        self.set_callback_predicate(callback_predicate)

    @property
    def apply_calibration(self) -> bool:
        """Return whether newly received CSI is calibrated before storage."""

        return self._apply_calibration

    @apply_calibration.setter
    def apply_calibration(self, enabled: bool) -> None:
        """Enable or disable calibration of newly received CSI."""

        self._apply_calibration = bool(enabled)

    def set_callback_predicate(self, callback_predicate=None) -> None:
        """Replace the CSI completion predicate used by this backlog."""

        if self._callback_handle is None:
            self._callback_handle = self._pool.add_csi_callback(
                self._on_new_csi,
                callback_predicate=callback_predicate,
            )
        else:
            self._callback_handle = self._pool.replace_csi_callback(
                self._callback_handle,
                self._on_new_csi,
                callback_predicate=callback_predicate,
            )

    def _deserialize_csi_field(
        self,
        clustered_csi,
        field,
        enabled_fields,
        available_property,
        deserialize_method,
        calibration_method,
        values,
    ) -> None:
        if field not in enabled_fields:
            return
        if not getattr(clustered_csi, available_property):
            self._logger.debug(
                "Received a CSI cluster without %s even though that backlog field is enabled",
                field,
            )
            return

        csi = getattr(clustered_csi, deserialize_method)()
        if self._apply_calibration:
            calibration = self._pool.calibration
            if calibration is None:
                raise RuntimeError("CSI calibration is enabled but the pool has no calibration")
            csi = getattr(calibration, calibration_method)(csi)
        values[field] = csi

    def _on_new_csi(self, clustered_csi):
        """Translate one accepted CSI cluster into a backlog datapoint."""

        if not self._passes_filters(clustered_csi):
            return

        fields = self.fields
        values = {}

        if "timestamp" in fields:
            values["timestamp"] = clustered_csi.sensor_timestamps
        if "host_timestamp" in fields:
            values["host_timestamp"] = clustered_csi.host_timestamp

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

        metadata_properties = {
            "rssi": "rssi",
            "rx_gain": "rx_gain",
            "fft_gain": "fft_gain",
            "cfo": "cfo",
            "lltf_8bit_mode": "lltf_8bit_mode",
            "rf_switch_state": "rf_switch_state",
        }
        for field, property_name in metadata_properties.items():
            if field in fields:
                values[field] = getattr(clustered_csi, property_name)

        if "mac" in fields:
            mac_string = clustered_csi.source_mac
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
            "radar_tx_rf_switch_state",
        }
        if fields & radar_fields and clustered_csi.has_radar_tx_report:
            radar_tx_report = clustered_csi.radar_tx_report
            if "radar_tx_timestamp" in fields:
                values["radar_tx_timestamp"] = radar_tx_report.get_hardware_tx_timestamp_ns() / 1e9
            if "radar_tx_index" in fields:
                values["radar_tx_index"] = clustered_csi.radar_tx_index
            if "radar_tx_power" in fields:
                values["radar_tx_power"] = radar_tx_report.tx_power
            if "radar_tx_rf_switch_state" in fields:
                values["radar_tx_rf_switch_state"] = radar_tx_report.rf_switch_state

        self.append_datapoint(values)

    def start(self):
        """Start the pool's processing worker so completed clusters reach this backlog."""

        self._pool.start_processing()

    def stop(self):
        """Stop the pool-processing worker, if running."""

        self._pool.stop_processing()

    def close(self):
        """Stop processing and detach this backlog from its CSI pool."""

        self.stop()
        if self._callback_handle is not None:
            self._pool.remove_csi_callback(self._callback_handle)
            self._callback_handle = None
