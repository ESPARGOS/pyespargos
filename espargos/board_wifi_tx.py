"""Board scheduled WiFi transmission controls and report decoding.

ESPARGOS radar measurements use selected sensors to transmit scheduled WiFi
frames while the remaining sensors capture CSI.  The controller configuration
describes that transmission schedule, and each transmitting sensor emits a
report containing the result and precise transmit metadata.

``board.wifi_tx`` groups those transmit-side operations.  It deliberately does
not perform clustering or radar signal processing; those remain higher-level
responsibilities of :class:`espargos.csi_pool.CSIPool` and :mod:`espargos.radar`.
"""

from __future__ import annotations

from typing import Callable

from . import radar_packet
from . import sensor
from .board import BoardCapability, SensorMessageSubscription


class WiFiTx(BoardCapability):
    """Scheduled WiFi transmission controls for one board."""

    def set_config(self, config: dict):
        """Update the low-level per-sensor transmission schedule.

        Supported fields include ``active_by_antid``, ``start_by_antid``,
        ``period_by_antid``, ``mac_by_antid``, ``rfswitch_state``,
        ``tx_power``, ``tx_phymode``, and ``tx_rate``. Only provided fields are
        changed by the controller.
        """

        self.board.control.command("set_tx_control", config)

    def get_config(self) -> dict:
        """Return the low-level per-sensor transmission schedule."""

        return self.board.control.get_json("get_tx_control")

    def subscribe_reports(
        self,
        callback: Callable[
            [sensor.SensorMessage[radar_packet.RadarTxReportPacket]],
            None,
        ],
    ) -> SensorMessageSubscription:
        """Subscribe to decoded reports produced by transmitting sensors."""

        return self._subscribe_decoded_sensor_messages(
            radar_packet.RADAR_TX_REPORT_TYPE_HEADER,
            radar_packet.RadarTxReportPacket,
            callback,
        )
