"""BLE packet captures belonging to one over-the-air event on one board."""

import time
import numpy as np

from .ble_packet import BLEPacket
from .sensor_cluster import SensorCluster, ClusterCollisionError

__all__ = ["BLECluster"]


class BLECluster(SensorCluster):
    """Array-shaped raw captures, without calibration or phase estimation.

    BLE clock epochs and capture timestamps are board-local. A cluster therefore
    covers one board, even in a multi-board pool. Other board positions stay empty.
    Repeated PDU prefixes alone are not event identities.

    Packet identity includes its access address, decoded prefix and received CRC.
    ``crc_valid`` reports verification separately. Unknown connection CRC seeds
    cannot be verified. Missing positions remain NaN in numeric metadata.
    """

    def __init__(self, event_key, board_index, first_packet, board_revisions):
        super().__init__(board_revisions)
        if not first_packet.qualified:
            raise ValueError("A BLE cluster needs a qualified packet anchor")
        self.event_key = event_key
        self.board_index = board_index
        self.packet = first_packet
        self.settled = False
        self._host_timestamp = time.time()
        self._packets = np.full(self.shape, None, dtype=object)
        self._crc_valid = np.zeros(self.shape, bool)
        self._rx_gain = np.full(self.shape, np.nan)
        self._rf_switch_state = np.full(self.shape, 255, dtype=np.uint8)
        self._time_us = np.full(self.shape, np.nan)

    @property
    def is_complete(self):
        """Whether all eight sensors on the originating board contributed."""
        return bool(np.all(self.completion[self.board_index]))

    @property
    def host_timestamp(self):
        return self._host_timestamp

    @property
    def crc_valid(self):
        return self._crc_valid

    @property
    def rx_gain(self):
        """Measured packet gain, or NaN when it was unavailable/unstable."""
        return self._rx_gain

    @property
    def rf_switch_state(self):
        """Effective per-capture input, including independently randomized sensors."""
        return self._rf_switch_state

    @property
    def time_us(self):
        return self._time_us

    @property
    def is_calibration(self):
        return self.packet.rf_switch == 1

    def get_packet(self, board_index, antenna_id):
        """Return one sensor capture, preserving its variable-length native IQ."""
        return self._packets[self.get_sensor_position(board_index, antenna_id)]

    def add_message(self, board_index, sensor_message):
        """Add a qualified packet with the same decoded identity and timing."""
        message = sensor_message
        p = message.payload
        if not isinstance(p, BLEPacket):
            raise TypeError("BLECluster requires BLEPacket payloads")
        anchor = self.packet
        if (
            board_index != self.board_index
            or not p.synchronized
            or (p.clock_epoch, p.generation, p.settings) != (anchor.clock_epoch, anchor.generation, anchor.settings)
        ):
            raise ClusterCollisionError("BLE capture belongs to a different board or receiver context")
        if p.qualified:
            if p.identity != anchor.identity:
                raise ClusterCollisionError("BLE packet identity differs from the anchor")
        else:
            raise ClusterCollisionError("BLE capture has no qualified packet identity")
        window_us = 2000
        if abs(p.time_us - anchor.time_us) > window_us or np.any(abs(self._time_us[self.completion] - p.time_us) > window_us):
            raise ClusterCollisionError("BLE capture falls outside this event's timing window")
        position = self.get_sensor_position(board_index, message.antenna_id)
        existing = self._packets[position]
        if existing is not None:
            if bytes(existing) == bytes(p):
                return False
            raise ClusterCollisionError("Different BLE captures occupy the same sensor position")
        self._packets[position] = p
        self._crc_valid[position] = p.crc_valid
        self._rx_gain[position] = p.rx_gain if p.rx_gain is not None else np.nan
        self._rf_switch_state[position] = p.rf_switch
        self._time_us[position] = p.time_us
        self._mark_sensor_position_complete(position)
        return True
