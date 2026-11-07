"""BLE configuration and packet-IQ delivery for one board."""

import json

from .board import BoardCapability, EspargosAPIVersionError
from .ble_packet import BLEPacket, BLE_PACKET_TYPE_HEADER

__all__ = ["BLECapability"]


class BLECapability(BoardCapability):
    """Access through ``board.ble``. Subscriptions never change radio modes."""

    CONFIG_KEYS = (
        "channel",
        "phy",
        "gain_mode",
        "rx_gain",
        "expert_gain_words",
        "rf_switch",
        "decimation",
    )

    def __init__(self, board):
        if board.api_version[0] < 5:
            raise EspargosAPIVersionError("BLE requires controller API 5 and BLQ5 sensor firmware")
        super().__init__(board)

    @staticmethod
    def packet_settings(config):
        """Encode the receiver settings carried in a BLE packet header."""
        gain_mode = {"manual": 0, "auto": 1, "expert": 2}[config["gain_mode"]]
        return config["channel"] | config["rf_switch"] << 8 | config["rx_gain"] << 16 | gain_mode << 24

    def get_config(self):
        """Return live receiver settings, including their configuration generation."""
        return self._board.control.get_json("get_bluetooth")

    def set_config(self, config):
        """Apply partial board-wide BLE settings and return the resulting state.

        This does not select sensor modes or change persistent reference settings.
        The controller validates channel, PHY, gain and capture options.
        Full IQ candidates are streamed and decoded on the host.
        """
        return json.loads(self._board.control.post_json("set_bluetooth", config))

    def get_reference_status(self):
        """Return the controller's BLE reference generator status."""
        return self._board.control.get_json("get_bluetooth_reference")

    def subscribe_packets(self, callback):
        """Deliver parsed BLEPacket payloads with their SensorMessage metadata.

        Includes host-decoded packets, undecodable candidates, and
        qualified non-advertising PDUs whose CRC seed is unknown.
        Qualification and CRC verification are separate packet properties.
        """
        return self._subscribe_decoded_sensor_messages(BLE_PACKET_TYPE_HEADER, BLEPacket, callback)
