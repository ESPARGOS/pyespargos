"""BLE configuration and packet-IQ delivery for one board."""

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
        common = config["channel"] | config["rx_gain"] << 16 | gain_mode << 24
        paths = config["rf_switch"]
        if isinstance(paths, list):
            return tuple(common | path << 8 for path in paths)
        return common | paths << 8

    def get_config(self):
        """Return live receiver settings, including their configuration generation."""
        return self._board._resources.read_radio("ble/rx")

    def set_config(self, config):
        """Update BLE receiver settings and return the resulting state.

        Use a single ``rf_switch`` value for all eight inputs or an antenna-ID
        mapping for individual inputs.
        The controller validates channel, PHY, gain and capture options.
        Sensors normally qualify candidates before sending their IQ. Set
        ``sensor_filter=False`` to receive all energy-triggered captures.
        """
        return self._board._resources.write_radio("ble/rx", config)

    def get_reference_status(self):
        """Return the controller's BLE reference generator status."""
        return self._board._resources.read("reference/ble")

    def subscribe_packets(self, callback):
        """Deliver parsed BLEPacket payloads with their SensorMessage metadata.

        Includes host-decoded packets, undecodable candidates, and
        qualified non-advertising PDUs whose CRC seed is unknown.
        Qualification and CRC verification are separate packet properties.
        """
        return self._subscribe_decoded_sensor_messages(BLE_PACKET_TYPE_HEADER, BLEPacket, callback)
