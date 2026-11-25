"""Controller configuration and subscriptions for WiFi IQ capture."""

import json

from ..board import BoardCapability
from .packet import RECORD_TYPES, WiFiIQPacket


class WiFiIQCapability(BoardCapability):
    def get_config(self):
        return self._board.control.get_json("get_wifi_iq")

    def set_config(self, settings):
        return json.loads(self._board.control.post_json("set_wifi_iq", settings))

    def subscribe_packets(self, callback):
        """Deliver all capture and metadata record types with antenna IDs."""
        headers = tuple(int.from_bytes(tag, "little") for tag in RECORD_TYPES)
        return self._subscribe_decoded_sensor_messages(headers, WiFiIQPacket, callback)
