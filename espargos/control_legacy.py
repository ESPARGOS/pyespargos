"""Adapters for controllers that predate the versioned control API."""

import json

from .board import EspargosAPIVersionError
from .control_api import _sensor_inputs, _input_name
from .sensor import RFSwitchState


def check_iq_rx_settings(api_major, settings):
    if api_major >= 5:
        return
    trigger = settings.get("trigger")
    downsample = isinstance(trigger, dict) and trigger.get("mode") == "downsample"
    bluetooth = settings.get("capture_path") == "bluetooth" or any(key.startswith("bluetooth_") for key in settings)
    if bluetooth or downsample:
        raise EspargosAPIVersionError(f"Controller API {api_major} supports only WiFi IQ capture. Bluetooth IQ capture and continuous downsampling require API 5 or later.")


def iq_tx_settings(api_version, tx_status, settings):
    if "sample_rate_hz" not in settings:
        return settings
    # API 5.0 gained writable TX rates and per-sensor rate readback together.
    if api_version > (5, 0) or (api_version == (5, 0) and any("sample_rate_hz" in sensor for sensor in tx_status["sensors"])):
        return settings
    if settings["sample_rate_hz"] != 80_000_000:
        raise EspargosAPIVersionError("This controller has a fixed IQ transmit rate of 80 MSa/s. Update the firmware to select another rate.")
    return {key: value for key, value in settings.items() if key != "sample_rate_hz"}


class LegacyControllerResources:
    def __init__(self, control, *, api_major=5):
        self.control = control
        self.api_major = api_major

    per_sensor_paths = False
    _radio = {"iq/rx": "iq_control", "wifi_iq/rx": "wifi_iq", "ble/rx": "bluetooth", "wifi/tx": "tx_control"}

    def read_radio(self, resource):
        return self.control.get_json("get_" + self._radio[resource])

    def write_radio(self, resource, settings):
        if resource == "iq/rx":
            check_iq_rx_settings(self.api_major, settings)
        if resource == "wifi_iq/rx":
            for field in ("gain_mode", "rx_gain", "expert_gain_words"):
                value = settings.get(field)
                if isinstance(value, dict) or (isinstance(value, (list, tuple)) and (field != "expert_gain_words" or len(value) != 3)):
                    raise NotImplementedError("This controller supports only a shared WiFi IQ gain")
        changes = settings.copy()
        key = "rfswitch_state" if resource == "wifi/tx" else "rf_switch"
        if key in changes:
            if changes[key] == {}:
                del changes[key]
            else:
                changes[key] = self._common_input(changes[key])
        response = self.control.post_json("set_" + self._radio[resource], changes)
        return None if response == "ok" else json.loads(response)

    # Most v5 resources were a pair of commands sharing a suffix.
    _paths = {
        "device/reboot": (None, "reboot"),
        "reference/wifi": ("get_wifi_runtime", "set_wifi_reference_config"),
        "wifi/tune": (None, "set_wifi_runtime"),
        "reference/ble": ("get_bluetooth_reference", "set_bluetooth_reference"),
        "reference/iq": ("get_iq_reference", "set_iq_reference"),
        "reference/iq/gate": (None, "gate_iq_reference"),
        "roles/controller": ("get_controller_mode", "set_controller_mode"),
        "iq/rx/status": ("get_iq_rx_status", None),
        "iq/rx/signal/ack": (None, "ack_iq_signal"),
        "iq/rx/sync-anchor": (None, "set_iq_sync_anchor"),
        "iq/tx": ("get_iq_tx", "set_iq_tx"),
        "iq/tx/synthesize": (None, "set_iq_tx_waveform"),
        "iq/monitor": ("get_iq_monitor", "set_iq_monitor"),
        "iq/tx/stream": ("get_iq_tx_stream_config", "set_iq_tx_stream_config"),
        "config/transmit-authorization": ("get_transmit_authorization", "set_transmit_authorization"),
    }
    _wifi_fields = {"gain": "gain_settings", "csi": "csi_acquire_config", "mac_filter": "mac_filter", "cfo": "cfo_correction"}

    @staticmethod
    def decode(resource, state):
        return state

    def read(self, resource):
        from .board import EspargosHTTPStatusError

        try:
            return self.control.get_json(self._paths[resource][0])
        except EspargosHTTPStatusError as exc:
            if resource != "reference/wifi" or exc.status != 404 or self.api_major >= 5:
                raise
            return self.control.get_json("get_wificonf")

    def post(self, resource, settings):
        return self.control.post_json(self._paths[resource][1], settings)

    def command(self, resource, settings):
        from .board import EspargosHTTPStatusError

        try:
            return self.control.command(self._paths[resource][1], settings)
        except EspargosHTTPStatusError as exc:
            if resource != "wifi/tune" or exc.status != 404 or self.api_major >= 5:
                raise
            return self.control.command("set_wificonf", settings)

    def upload(self, data):
        return self.control.fetch("upload_iq_tx_waveform", data)

    def wifi_field(self, field):
        value = self.control.get_json("get_" + self._wifi_fields[field])
        if field == "csi" and "lltf_8bit_mode" not in value and "lltf_bit_mode" in value:
            value["lltf_8bit_mode"] = value["lltf_bit_mode"]
        return value

    def set_wifi_field(self, field, value):
        if field == "csi" and "lltf_8bit_mode" in value:
            value = {"lltf_bit_mode": value["lltf_8bit_mode"], **value}
        self.control.command("set_" + self._wifi_fields[field], value)

    def write_wifi_selection(self, antennas, channel, paths):
        # v5 writes both full channel vectors, even when one antenna changes.
        if channel:
            state = self.wifi_channels()
            if not state["override_active"] and ("channel-primary" not in state or "channel-secondary" not in state):
                base = self.read("reference/wifi")
                state.update({key: [base[key]] * 8 for key in ("channel-primary", "channel-secondary")})
            state["override_active"] = True
            for field, value in channel.items():
                for a in antennas:
                    state["channel-" + field][a] = value
            self.set_wifi_channels(state)
        if paths is not None:
            self.set_wifi_inputs(paths)

    def wifi_channels(self):
        return self.control.get_json("get_wifi_channel_overrides")

    def set_wifi_channels(self, value):
        self.control.command("set_wifi_channel_overrides", value)

    def network(self):
        return self.control.get_json("get_netconf")

    def network_status(self):
        return self.control.get_json("get_ip_info")

    def wifi_inputs(self):
        return RFSwitchState(int(self.control.fetch("get_rfswitch")))

    @staticmethod
    def _common_input(state):
        if isinstance(state, (tuple, list)):
            state = dict(enumerate(state))
        if isinstance(state, dict):
            updates = _sensor_inputs(state)
            if len(updates) != 8 or len(set(updates.values())) != 1:
                raise NotImplementedError("This controller only supports setting the RF path for all sensors together")
            state = next(iter(state.values()))
        _input_name(state)
        return int(state)

    def set_wifi_inputs(self, state):
        if state != {}:
            self.control.command("set_rfswitch", self._common_input(state))
