"""Controller configuration and subscriptions for WiFi IQ capture."""

from ..board import BoardCapability
from .packet import RECORD_TYPES, WiFiIQPacket


class WiFiIQCapability(BoardCapability):
    def get_config(self):
        return self._board._resources.read_radio("wifi_iq/rx")

    def set_config(self, settings):
        return self._board._resources.write_radio("wifi_iq/rx", settings)

    def subscribe_packets(self, callback):
        """Deliver all capture and metadata record types with antenna IDs."""
        headers = tuple(int.from_bytes(tag, "little") for tag in RECORD_TYPES)
        return self._subscribe_decoded_sensor_messages(headers, WiFiIQPacket, callback)
