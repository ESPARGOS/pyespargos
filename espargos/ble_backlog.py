"""Thread-safe array-shaped BLE measurement storage, parallel to CSI/IQ backlogs."""

import logging
import numpy as np
from .sensor_backlog import SensorBacklog, BacklogField


class BLEBacklog(SensorBacklog):
    """Retain selected raw fields from delivered BLE clusters.

    This subscribes without selecting advertisers, changing receiver settings or
    computing phase. Variable-length packet captures are stored as BLEPacket
    objects in the optional ``packets`` field. Missing sensors contain None.
    Use the shared filter chain to restrict sources or packet types. For the
    short covariance/polarization window, use BLEPhaseHistory separately.
    """

    FIELD_SPECS = {
        "packets": BacklogField((), object, per_sensor=True, fill_value=None),
        "completion": BacklogField((), bool, per_sensor=True, fill_value=False),
        "crc_valid": BacklogField((), bool, per_sensor=True, fill_value=False),
        "rx_gain": BacklogField((), np.float64, per_sensor=True, fill_value=np.nan),
        "rf_switch_state": BacklogField((), np.uint8, per_sensor=True, fill_value=255),
        "time_us": BacklogField((), np.float64, per_sensor=True, fill_value=np.nan),
        "host_timestamp": BacklogField((), np.float64, per_sensor=False, fill_value=np.nan),
        "board_index": BacklogField((), np.int64, per_sensor=False, fill_value=-1),
    }

    def __init__(self, receiver, fields=None, callback_predicate=None, include_partial=False, size=100):
        self._receiver = receiver
        super().__init__(sensor_shape=receiver.shape, field_specs=self.FIELD_SPECS,
                         fields=fields, size=size, logger=logging.getLogger("pyespargos.ble_backlog"))
        self._callback_handle = receiver.add_callback(self._on_cluster,
            callback_predicate=callback_predicate, include_partial=include_partial)

    def _on_cluster(self, cluster):
        if not self._passes_filters(cluster):
            return
        fields = self.fields
        values = {key: getattr(cluster, key) for key in fields if key != "packets"}
        if "packets" in fields:
            packets = np.full(cluster.shape, None, dtype=object)
            for antenna in range(8):
                position = cluster.get_sensor_position(cluster.board_index, antenna)
                packets[position] = cluster.get_packet(cluster.board_index, antenna)
            values["packets"] = packets
        self.append_datapoint(values)

    def close(self):
        """Detach this consumer without stopping the receiver or other backlogs."""
        if self._callback_handle is not None:
            self._receiver.remove_callback(self._callback_handle)
            self._callback_handle = None
