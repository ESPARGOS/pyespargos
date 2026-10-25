"""Scheduled WiFi transmission and decoded TX reports for one board.

Access through board.wifi_tx. Pool.wifi associates TX reports with received CSI.
"""

from __future__ import annotations

from typing import Callable

from . import wifi_tx_packet
from . import sensor
from .board import BoardCapability, SensorMessageSubscription

__all__ = ["WiFiTxCapability"]


class WiFiTxCapability(BoardCapability):
    """Scheduled WiFi transmission controls for one board."""

    def set_config(self, config: dict):
        """Update the low-level per-sensor transmission schedule.

        Supported fields include ``active_by_antid``, ``start_by_antid``,
        ``period_by_antid``, ``mac_by_antid``, ``rfswitch_state``,
        ``tx_power``, ``tx_phymode``, and ``tx_rate``. Only provided fields are
        changed by the controller.
        """

        self._board.control.command("set_tx_control", config)

    def get_config(self) -> dict:
        """Return the low-level per-sensor transmission schedule."""

        return self._board.control.get_json("get_tx_control")

    def subscribe_reports(
        self,
        callback: Callable[
            [sensor.SensorMessage[wifi_tx_packet.WiFiTxReportPacket]],
            None,
        ],
    ) -> SensorMessageSubscription:
        """Subscribe to decoded reports produced by transmitting sensors."""

        return self._subscribe_decoded_sensor_messages(
            wifi_tx_packet.WIFI_TX_REPORT_TYPE_HEADER,
            wifi_tx_packet.WiFiTxReportPacket,
            callback,
        )
