"""WiFi receive controls and decoded CSI delivery for one board.

Access through board.wifi_rx. Pool.wifi assembles messages into CSI clusters.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from . import cfo
from . import csi_packet
from . import sensor
from .board import (
    BoardCapability,
    SensorMessageSubscription,
)

__all__ = ["WiFiRxCapability"]


class WiFiRxCapability(BoardCapability):
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

    def set_rf_switch(self, state: sensor.RFSwitchState | dict[int, sensor.RFSwitchState]):
        """Set every WiFi input, or update the antennas named in a mapping."""
        self._board._resources.set_wifi_inputs(state)

    def get_rf_switch(self) -> sensor.RFSwitchState | dict[int, sensor.RFSwitchState]:
        """Return the common input, or a mapping by antenna ID for mixed inputs."""
        return self._board._resources.wifi_inputs()

    def set_mac_filter(self, mac_filter: dict):
        """Only receive frames whose sender MAC matches ``mac_filter``.

        ``mac_filter`` contains ``enable``, ``mac``, and optionally
        ``mac_mask``. MAC values use the ``"00:11:22:33:44:55"`` notation.
        """

        self._board._resources.set_wifi_field("mac_filter", mac_filter)

    def get_mac_filter(self) -> dict:
        """Return the current sender-MAC filter configuration."""

        return self._board._resources.wifi_field("mac_filter")

    def clear_mac_filter(self):
        """Disable sender-MAC filtering."""

        self._board._resources.set_wifi_field("mac_filter", {"enable": False})

    def set_config(self, config: dict):
        """Update the board's live WiFi receive and calibration configuration.

        Supported keys include ``channel-primary``, ``channel-secondary``,
        ``country-code``, and the ``calib-*`` reference-signal settings. Only
        provided fields are changed by the controller. This command also
        resets sensor channel overrides to the board's primary/secondary
        channel. Use :meth:`set_reference_config` to leave sensors tuned.

        This changes live settings on API 5 and later. Earlier controllers
        that only expose saved WiFi settings also write them to flash.
        """

        self._board._resources.command("wifi/tune", config)

    def get_config(self) -> dict:
        """Return the live WiFi receive and calibration configuration.

        The SDK reads the current reference profile and uses the saved profile
        on earlier firmware that only exposes saved WiFi settings. Saved boot defaults are not used for measurements.
        """

        return self._board._resources.read("reference/wifi")

    def set_reference_config(self, config: dict):
        """Update the controller's WiFi reference without retuning sensors.

        Accepts the same fields as :meth:`set_config`. Sensor channel settings
        stay unchanged, including when they previously matched the controller.
        Use :meth:`get_config` to read the resulting reference configuration and
        :meth:`get_channel_overrides` for the actual sensor channels.
        """
        self._board._resources.command("reference/wifi", config)

    def set_csi_acquisition_config(self, config: dict):
        """Update which WiFi training fields the sensors acquire as CSI.

        The configuration controls legacy/HT/HE acquisition, forced L-LTF,
        CSI compression, value scaling, ACK capture, and L-LTF bit width. Only
        provided fields are changed by the controller.
        """

        self._board._resources.set_wifi_field("csi", config)

    def get_csi_acquisition_config(self) -> dict:
        """Return the current CSI acquisition configuration."""

        return self._board._resources.wifi_field("csi")

    def set_cfo_correction(self, auto: bool, value: int = 0):
        """Configure automatic or fixed receiver frequency-offset correction.

        A fixed ``value`` is the signed 13-bit NRXFOE ``reg_foe_force`` field
        and must be in the range -4096 through 4095. For Hz, use
        :meth:`set_cfo_correction_hz`.
        """

        self._board._resources.set_wifi_field(
            "cfo",
            {"auto": bool(auto), "value": int(value)},
        )

    def get_cfo_correction(self) -> dict:
        """Return the receiver frequency-offset correction configuration."""

        return self._board._resources.wifi_field("cfo")

    def set_cfo_correction_hz(self, auto: bool, value_hz: float = 0):
        """Select automatic CFO correction or a fixed CFO in Hz.

        Use the sign of received packet ``rx_ctrl.cfo`` values. Manual 0 Hz
        disables correction for shared-clock reference/WiFi transmitters.
        Values are rounded to the nearest hardware step (about 76.3 Hz).
        Invalid or out-of-range values raise :class:`ValueError`.
        """
        self.set_cfo_correction(auto, cfo.correction_hz_to_raw(value_hz))

    def get_cfo_correction_hz(self) -> dict:
        """Return ``auto`` and the quantized ``value_hz`` stored by the receiver."""
        config = self.get_cfo_correction()
        return {"auto": config["auto"], "value_hz": cfo.correction_raw_to_hz(config["value"])}

    def _gain_value_for_controller(self, key: str, values):
        if isinstance(values, (str, bytes)):
            return values
        if np.asarray(values).ndim == 0:
            return values
        return self._board.revision.sensor_values_to_antenna_id_list(values, name=key)

    def set_gain_settings(self, settings: dict):
        """Configure automatic, fixed, or per-sensor receive gain.

        Gain and FFT-scale values may be scalars or board-local arrays with
        shape ``(2, 4)``. Arrays are converted to firmware antenna-ID order
        using the detected board revision.
        """

        payload = {key: self._gain_value_for_controller(key, value) for key, value in settings.items()}
        self._board._resources.set_wifi_field("gain", payload)

    def get_gain_settings(self) -> dict:
        """Return the current receiver gain settings."""

        return self._board._resources.wifi_field("gain")

    def set_channel_overrides(self, settings: dict):
        """Configure optional per-sensor primary and secondary channels.

        The controller accepts ``override_active`` plus ``channel-primary`` and
        ``channel-secondary`` lists in firmware antenna-ID order.
        """

        self._board._resources.set_wifi_channels(settings)

    def get_channel_overrides(self) -> dict:
        """Return the current per-sensor channel overrides."""

        return self._board._resources.wifi_channels()

    def subscribe_csi(
        self,
        callback: Callable[[sensor.SensorMessage[csi_packet.CSIPacket]], None],
    ) -> SensorMessageSubscription:
        """Subscribe to decoded CSI messages while preserving sensor metadata."""

        return self._subscribe_decoded_sensor_messages(
            csi_packet.CSI_TYPE_HEADER,
            csi_packet.CSIPacket,
            callback,
        )
