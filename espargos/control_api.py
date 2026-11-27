"""Board-local adapters for versioned controller resources."""

import json

from .sensor import RFSwitchState

_INPUTS = ("isolation", "reference", "right", "left", "random")


def _input_name(state):
    value = RFSwitchState(state)
    if value == RFSwitchState.SENSOR_RFSWITCH_UNKNOWN:
        raise ValueError("Unknown RF input cannot be selected")
    return _INPUTS[value]


def _sensor_inputs(states):
    updates = {}
    for antenna, state in states.items():
        if type(antenna) is not int or not 0 <= antenna < 8:
            raise ValueError("Antenna ID must be an integer from 0 to 7")
        updates[antenna] = _input_name(state)
    return updates


def _paths_payload(key, state):
    if isinstance(state, (tuple, list)):
        if len(state) != 8:
            raise ValueError("Expected one RF path for each of the eight sensors")
        state = dict(enumerate(state))
    if isinstance(state, dict):
        if not state:
            return {}
        return {"sensors": [{"antid": i, key: name} for i, name in _sensor_inputs(state).items()]}
    return {key: _input_name(state)}


_TX_FIELDS = {"active_by_antid": "active", "start_by_antid": "start_us",
              "period_by_antid": "period_us", "mac_by_antid": "mac"}


def _decode_radio(resource, result):
    key = "output" if resource == "wifi/tx" else "input"
    switch = "rfswitch_state" if key == "output" else "rf_switch"
    if resource == "wifi/tx" and "sensors" in result:
        entries = sorted(result.pop("sensors"), key=lambda e: e["antid"])
        for field, wire in _TX_FIELDS.items():
            result[field] = [entry[wire] for entry in entries]
        if key not in result:
            result[switch] = [_INPUTS.index(entry[key]) for entry in entries]
    if resource == "iq/rx" and "sensors" in result:
        entries = result.pop("sensors")
        if key not in result:
            result[switch] = [_INPUTS.index(entry.pop(key)) for entry in sorted(entries, key=lambda e: e["antid"])]
        result["receivers"] = entries
    if key in result:
        result[switch] = _INPUTS.index(result.pop(key))
    elif "sensors" in result:
        entries = result.pop("sensors")
        paths = {entry["antid"]: _INPUTS.index(entry[key]) for entry in entries}
        result[switch] = [paths[i] for i in range(8)]
    return result


class ControllerResources:
    def __init__(self, control, *, minor=1):
        self.minor = minor
        self.control = control

    per_sensor_paths = True

    def read_radio(self, resource):
        return _decode_radio(resource, self.control.get_json("api/v6/" + resource))

    def write_radio(self, resource, settings):
        changes = settings.copy()
        key = "output" if resource == "wifi/tx" else "input"
        switch = "rfswitch_state" if key == "output" else "rf_switch"
        if switch in changes:
            changes.update(_paths_payload(key, changes.pop(switch)))
        if resource == "wifi/tx" and changes.keys() & _TX_FIELDS.keys():
            updates = {s["antid"]: s for s in changes.pop("sensors", ())}
            for field, wire in _TX_FIELDS.items():
                if field not in changes:
                    continue
                values = changes.pop(field)
                if len(values) != 8:
                    raise ValueError(f"{field} needs eight transmitter values")
                for a, value in enumerate(values):
                    updates.setdefault(a, {"antid": a})[wire] = value
            changes["sensors"] = list(updates.values())
        if resource == "iq/rx" and "receivers" in changes:
            rows = changes.pop("receivers")
            # A caller may set gain and polarization on the same antenna.
            paths = {r["antid"]: r[key] for r in changes.pop("sensors", ())}
            sensors = []
            for row in rows:
                row = dict(row)
                if row["antid"] in paths:
                    row[key] = paths.pop(row["antid"])
                sensors.append(row)
            sensors.extend({"antid": antenna, key: path} for antenna, path in paths.items())
            if sensors:
                changes["sensors"] = sensors
        response = self.control.post_json("api/v6/" + resource, changes)
        if response == "ok":
            return None
        return _decode_radio(resource, json.loads(response))

    # The SDK historically calls the TX outlets antenna_r, antenna_l and isolated.
    # Keep those names in transmit recipes and calibration snapshots.
    _tx_output = {"antenna_r": "right", "antenna_l": "left", "isolated": "isolation", "reference": "reference"}

    def decode(self, resource, state):
        if resource == "iq/tx":
            outlets = {wire: name for name, wire in self._tx_output.items()}
            state.setdefault("output", None)
            for entry in [state, *state.get("sensors", ())]:
                if entry.get("output") is not None:
                    entry["output"] = outlets[entry["output"]]
        return state

    def read(self, resource):
        if resource == "iq/tx/stream" and self.minor == 0:
            resource = "config/iq-tx-stream"
        return self.decode(resource, self.control.get_json("api/v6/" + resource))

    def post(self, resource, settings):
        if resource == "iq/tx":
            def output(entry):
                if "output" not in entry:
                    return entry
                return {**entry, "output": self._tx_output.get(entry["output"], entry["output"])}

            settings = dict(output(settings))
            if "transmitters" in settings:
                settings["sensors"] = [output(t) for t in settings.pop("transmitters")]
        return self.control.post_json("api/v6/" + resource, settings)

    def command(self, resource, settings):
        return self.control.command("api/v6/" + resource, settings)

    def upload(self, data):
        return self.control.fetch("api/v6/iq/tx/upload", data)

    def wifi_field(self, field):
        settings = self.read("wifi/rx")
        if field == "gain" and field not in settings:
            rows = sorted(settings["sensors"], key=lambda s: s["antid"])
            return {key: [row["gain"][key] for row in rows] for key in rows[0]["gain"]}
        return settings[field]

    def set_wifi_field(self, field, value):
        if field == "gain" and any(isinstance(v, list) for v in value.values()):
            if any(len(v) != 8 for v in value.values() if isinstance(v, list)):
                raise ValueError("Expected eight per-sensor gain values")
            common = {key: v for key, v in value.items() if not isinstance(v, list)}
            columns = {key: v for key, v in value.items() if isinstance(v, list)}
            self.command("wifi/rx", {**({"gain": common} if common else {}), "sensors": [
                {"antid": a, "gain": {key: v[a] for key, v in columns.items()}} for a in range(8)]})
        else:
            self.command("wifi/rx", {field: value})

    def write_wifi_selection(self, antennas, channel, paths):
        settings = {"channel": channel} if channel else {}
        if len(antennas) == 8 and not isinstance(paths, dict):
            if paths is not None:
                settings["input"] = _input_name(paths)
        else:
            rows = []
            for antenna in antennas:
                row = {"antid": antenna, **settings}
                if paths is not None:
                    row["input"] = _input_name(paths[antenna] if isinstance(paths, dict) else paths)
                rows.append(row)
            settings = {"sensors": rows}
        self.command("wifi/rx", settings)

    def wifi_channels(self):
        state = self.read("wifi/rx")
        ref = self.read("reference/wifi")
        channels = ([state["channel"]] * 8 if "channel" in state else
                    [s["channel"] for s in sorted(state["sensors"], key=lambda s: s["antid"])])
        primary = [c["primary"] for c in channels]
        secondary = [c["secondary"] for c in channels]
        return {"channel-primary": primary, "channel-secondary": secondary, "override_active":
                any(p != ref["channel-primary"] or s != ref["channel-secondary"] for p, s in zip(primary, secondary))}

    def set_wifi_channels(self, value):
        if not value["override_active"]:
            ref = self.read("reference/wifi")
            self.command("wifi/rx", {"channel": {"primary": ref["channel-primary"], "secondary": ref["channel-secondary"]}})
            return
        def channels(key):
            items = value["channel-" + key]
            if isinstance(items, int):
                return [items] * 8
            if len(items) != 8:
                raise ValueError(f"Expected eight {key} channels")
            return items
        pairs = zip(channels("primary"), channels("secondary"))
        self.command("wifi/rx", {"sensors": [{"antid": a, "channel": {"primary": p, "secondary": s}}
                                             for a, (p, s) in enumerate(pairs)]})

    def network(self):
        return self.control.get_json("api/v6/config/network")

    def network_status(self):
        return self.control.get_json("api/v6/network/status")

    def wifi_inputs(self):
        config = self.control.get_json("api/v6/wifi/rx")
        if "input" in config:
            return RFSwitchState(_INPUTS.index(config["input"]))
        return {entry["antid"]: RFSwitchState(_INPUTS.index(entry["input"])) for entry in config["sensors"]}

    def set_wifi_inputs(self, state):
        self.control.command("api/v6/wifi/rx", _paths_payload("input", state))
