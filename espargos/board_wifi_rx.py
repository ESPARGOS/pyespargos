"""Board WiFi receive controls and CSI sensor-message decoding.

An ESPARGOS board can receive WiFi frames on all of its sensors and report
their channel state information (CSI).  This capability owns the controller
settings that affect that receive path—channel selection, CSI acquisition, RF
switches, filtering, frequency correction, and gain—and turns raw
sensor-message payloads into decoded CSI packets.

Keeping this API on ``board.wifi_rx`` leaves :class:`espargos.board.Board`
responsible only for controller identity, transport, and generic sensor-message
delivery.  Other acquisition modes can therefore add their own capabilities
without growing the Board interface.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from . import csi_packet
from . import sensor
from .board import (
    BoardCapability,
    EspargosUnexpectedResponseError,
    SensorMessageSubscription,
)


class WiFiRx(BoardCapability):
    """WiFi receive configuration and decoded CSI delivery for one board."""

    DEFAULT_CSI_ACQUISITION_CONFIG = {
        "enable": True,
        "acquire_csi_legacy": True,
        "acquire_csi_force_lltf": False,
        "compress_csi": False,
        "acquire_csi_ht20": True,
        "acquire_csi_ht40": True,
        "acquire_csi_vht": True,
        "acquire_csi_su": True,
        "acquire_csi_mu": True,
        "acquire_csi_dcm": True,
        "acquire_csi_beamformed": True,
        "acquire_csi_he_stbc_mode": 2,
        "val_scale_cfg": 2,
        "dump_ack_en": True,
        "lltf_8bit_mode": False,
    }

    DEFAULT_CFO_CORRECTION = {
        "auto": True,
        "value": 0,
    }

    DEFAULT_GAIN_SETTINGS = {
        "fft_scale_enable": False,
        "fft_scale_value": 0,
        "rx_gain_enable": False,
        "rx_gain_value": 0,
    }

    DEFAULT_CHANNEL_OVERRIDES = {
        "override_active": False,
        "channel-primary": [1] * 8,
        "channel-secondary": [0] * 8,
    }

    def set_rf_switch(self, state: sensor.RFSwitchState):
        """Set the receive-path RF switch to an antenna or reference input."""

        response = self.board.control.fetch(
            "set_rfswitch",
            str(int(state)),
        )
        if response != "ok":
            self.board.logger.error(f"Invalid response: {response}")
            raise EspargosUnexpectedResponseError(str(response))

    def get_rf_switch(self) -> sensor.RFSwitchState:
        """Return the current receive-path RF switch state."""

        response = self.board.control.fetch("get_rfswitch")
        try:
            return sensor.RFSwitchState(int(response))
        except ValueError:
            self.board.logger.error(f"Invalid response: {response}")
            raise EspargosUnexpectedResponseError(str(response))

    def set_mac_filter(self, mac_filter: dict):
        """Only receive frames whose sender MAC matches ``mac_filter``.

        ``mac_filter`` contains ``enable``, ``mac``, and optionally
        ``mac_mask``. MAC values use the ``"00:11:22:33:44:55"`` notation.
        """

        self.board.control.command("set_mac_filter", mac_filter)

    def get_mac_filter(self) -> dict:
        """Return the current sender-MAC filter configuration."""

        return self.board.control.get_json("get_mac_filter")

    def clear_mac_filter(self):
        """Disable sender-MAC filtering."""

        self.board.control.command("set_mac_filter", {"enable": False})

    def set_config(self, config: dict):
        """Update the board's WiFi receive and calibration configuration.

        Supported keys include ``channel-primary``, ``channel-secondary``,
        ``country-code``, and the ``calib-*`` reference-signal settings. Only
        provided fields are changed by the controller.
        """

        self.board.control.command("set_wificonf", config)

    def get_config(self) -> dict:
        """Return the board's WiFi receive and calibration configuration."""

        return self.board.control.get_json("get_wificonf")

    def set_csi_acquisition_config(self, config: dict):
        """Update which WiFi training fields the sensors acquire as CSI.

        The configuration controls legacy/HT/HE acquisition, forced L-LTF,
        CSI compression, value scaling, ACK capture, and L-LTF bit width. Only
        provided fields are changed by the controller.
        """

        payload = dict(config)
        if "lltf_8bit_mode" in payload and "lltf_bit_mode" not in payload:
            payload["lltf_bit_mode"] = payload["lltf_8bit_mode"]
        self.board.control.command("set_csi_acquire_config", payload)

    def get_csi_acquisition_config(self) -> dict:
        """Return the current CSI acquisition configuration."""

        config = self.board.control.get_json("get_csi_acquire_config")
        if "lltf_8bit_mode" not in config and "lltf_bit_mode" in config:
            config["lltf_8bit_mode"] = config["lltf_bit_mode"]
        return config

    def set_cfo_correction(self, auto: bool, value: int = 0):
        """Configure automatic or fixed receiver frequency-offset correction.

        A fixed ``value`` is the signed 13-bit NRXFOE ``reg_foe_force`` field
        and must be in the range -4096 through 4095.
        """

        self.board.control.command(
            "set_cfo_correction",
            {"auto": bool(auto), "value": int(value)},
        )

    def get_cfo_correction(self) -> dict:
        """Return the receiver frequency-offset correction configuration."""

        return self.board.control.get_json("get_cfo_correction")

    def _gain_value_for_controller(self, key: str, values):
        if isinstance(values, (str, bytes)):
            return values
        if np.asarray(values).ndim == 0:
            return values
        return self.board.revision.sensor_values_to_antid_list(values, name=key)

    def set_gain_settings(self, settings: dict):
        """Configure automatic, fixed, or per-sensor receive gain.

        Gain and FFT-scale values may be scalars or board-local arrays with
        shape ``(2, 4)``. Arrays are converted to firmware antenna-ID order
        using the detected board revision.
        """

        payload = {key: self._gain_value_for_controller(key, value) for key, value in settings.items()}
        self.board.control.command("set_gain_settings", payload)

    def get_gain_settings(self) -> dict:
        """Return the current receiver gain settings."""

        return self.board.control.get_json("get_gain_settings")

    def set_channel_overrides(self, settings: dict):
        """Configure optional per-sensor primary and secondary channels.

        The controller accepts ``override_active`` plus ``channel-primary`` and
        ``channel-secondary`` lists in firmware antenna-ID order.
        """

        self.board.control.command("set_wifi_channel_overrides", settings)

    def get_channel_overrides(self) -> dict:
        """Return the current per-sensor channel overrides."""

        return self.board.control.get_json("get_wifi_channel_overrides")

    def subscribe_csi(
        self,
        callback: Callable[[sensor.SensorMessage[csi_packet.CSIPacket]], None],
    ) -> SensorMessageSubscription:
        """Subscribe to decoded CSI messages while preserving sensor metadata."""

        return self._subscribe_decoded_sensor_messages(
            self.board.revision.csi_type_header,
            self.board.revision.csi_packet_type,
            callback,
        )
