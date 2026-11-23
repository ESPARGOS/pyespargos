"""Plan mode-specific settings before applying any writes across a selection.

Planners may read hardware but must not write it. They return a prepared update.
The common coordinator applies updates and mode changes, handles failure cleanup,
and notifies receivers. Measurement association and calibration validity remain
owned by receivers.
"""

from dataclasses import dataclass, field
from threading import RLock
from collections.abc import Callable
import math

from .calibration import Antenna
from .csi_processing import get_center_frequency
from .iq_packet import DECIM_TO_FS
from .iq_rx_stream import IQ_RX_STREAM_RATES, gain_changed, receive_sample_rate
from .iq_tx_control import _rpc, _wait

_ROUTES = {"isolated": 0, "reference": 1, "antenna_r": 2, "antenna_l": 3, "random": 4}
_PLANNERS = {}
_LOCK = RLock()


@dataclass(frozen=True)
class ConfigurationContext:
    pool: object
    board: object
    board_index: int
    antennas: tuple
    before: tuple
    modes: tuple
    snapshot: dict


@dataclass
class ConfigurationUpdate:
    """A validated update. Actions run before/after the board mode write.

    iq_settings merges into the firmware's shared IQ/mode control endpoint.
    Other capabilities use the action hooks. invalidated lists pool calibration
    identities affected by tuning. Receiver-local calibration is owned by the
    action or the receiver's modes_changed hook.
    """

    iq_settings: dict = field(default_factory=dict)
    before_modes: Callable | None = None
    after_modes: Callable | None = None
    invalidated: set = field(default_factory=set)


def register_configuration(mode, planner):
    if not isinstance(mode, str) or not mode.isidentifier() or mode.startswith("_") or not callable(planner):
        raise ValueError("A public mode name and callable configuration planner are required")
    with _LOCK:
        if mode in _PLANNERS and _PLANNERS[mode] is not planner:
            raise ValueError(f"Configuration planner for {mode!r} already registered")
        _PLANNERS[mode] = planner


def _validate(options, allowed):
    unsupported = options.keys() - set(allowed)
    if unsupported:
        raise ValueError(f"Unsupported settings for this sensor mode: {', '.join(sorted(unsupported))}")


def _identities(ctx):
    return {Antenna(ctx.board.host, a) for a in ctx.antennas}


def _board_wide(ctx):
    if len(ctx.antennas) != 8:
        raise ValueError("These receiver settings are board-wide. Select all eight sensors in this mode")


def _ble(ctx, options):
    from .ble_packet import channel_frequency

    _validate(options, ("channel", "frequency_hz", "rx_gain", "gain_mode", "output"))
    settings = {k: v for k, v in options.items() if k in ("channel", "rx_gain", "gain_mode")}
    if "rx_gain" in options:
        settings["gain_mode"] = options.get("gain_mode", "manual")
    if "output" in options:
        settings["rf_switch"] = _ROUTES[options["output"]]
    if "frequency_hz" in options:
        channel = next((c for c in range(40) if channel_frequency(c) == options["frequency_hz"]), None)
        if channel is None or "channel" in settings and settings["channel"] != channel:
            raise ValueError("BLE frequency must match a channel center")
        settings["channel"] = channel
    if "channel" in settings and (type(settings["channel"]) is not int or not 0 <= settings["channel"] <= 39):
        raise ValueError("BLE channel must be 0–39")
    if settings:
        _board_wide(ctx)

    def apply():
        receiver = ctx.pool._receivers.get("ble")
        if receiver is not None:
            receiver.apply_config(settings, board_index=ctx.board_index)
        else:
            ctx.board.ble.set_config(settings)

    return ConfigurationUpdate(before_modes=apply if settings else None)


def _wifi_channel(options):
    primary = options.get("channel")
    if "frequency_hz" in options:
        found = next((c for c in range(1, 15) if get_center_frequency(c) == options["frequency_hz"]), None)
        if found is None or primary is not None and primary != found:
            raise ValueError("WiFi frequency must match the primary channel center")
        primary = found
    if primary is not None and (type(primary) is not int or not 1 <= primary <= 14):
        raise ValueError("WiFi channel must be 1–14")
    return primary


def _wifi_iq(ctx, options):
    _validate(options, ("channel", "frequency_hz", "sample_rate_hz", "rx_gain", "gain_mode", "output", "boot_sync"))
    primary = _wifi_channel(options)
    settings = dict(options)
    settings.pop("frequency_hz", None)
    if primary is not None:
        settings["channel"] = primary
    if settings.pop("sample_rate_hz", 20e6) != 20e6:
        raise ValueError("WiFi IQ captures use 20 MSa/s")
    if "rx_gain" in settings:
        if type(settings["rx_gain"]) is not int or not 0 <= settings["rx_gain"] <= 76:
            raise ValueError("WiFi IQ gain must be 0–76")
        settings.setdefault("gain_mode", "manual")
    if "gain_mode" in settings and settings["gain_mode"] not in ("manual", "auto", "expert"):
        raise ValueError("Unknown WiFi IQ gain mode")
    if "boot_sync" in settings and type(settings["boot_sync"]) is not bool:
        raise ValueError("boot_sync must be a boolean")
    if "output" in settings:
        settings["rf_switch"] = _ROUTES[settings.pop("output")]
    if settings and any(mode == "wifi_iq" and a not in ctx.antennas for a, mode in enumerate(ctx.modes)):
        raise ValueError("Select every WiFi IQ sensor on the board to change shared capture settings")
    def apply():
        receiver = ctx.pool._receivers.get("wifi_iq")
        if receiver is None:
            ctx.board.wifi_iq.set_config(settings)
        else:
            receiver.configure(settings, board_index=ctx.board_index)

    return ConfigurationUpdate(before_modes=apply if settings else None)


def _wifi(ctx, options):
    _validate(options, ("frequency_hz", "channel", "secondary_channel", "output"))
    channels = None
    if any(k in options for k in ("frequency_hz", "channel", "secondary_channel")):
        primary = _wifi_channel(options)
        secondary = options.get("secondary_channel")
        if secondary not in (None, -1, 0, 1):
            raise ValueError("secondary_channel must be -1, 0 or 1")
        channels = ctx.board.wifi_rx.get_channel_overrides()
        if not channels["override_active"]:
            base = ctx.board.wifi_rx.get_config()
            channels = {"override_active": True, "channel-primary": [base["channel-primary"]] * 8, "channel-secondary": [base["channel-secondary"]] * 8}
        for a in ctx.antennas:
            if primary is not None:
                channels["channel-primary"][a] = primary
            if secondary is not None:
                channels["channel-secondary"][a] = 2 if secondary == -1 else secondary
    if "output" in options:
        _board_wide(ctx)

    def apply():
        if channels:
            receiver = ctx.pool._receivers.get("wifi")
            if receiver is not None:
                receiver.set_wifi_channel_overrides(channels, board_index=ctx.board_index, antennas=ctx.antennas)
            else:
                ctx.board.wifi_rx.set_channel_overrides(channels)
        if "output" in options:
            ctx.board.wifi_rx.set_rf_switch(_ROUTES[options["output"]])

    return ConfigurationUpdate(after_modes=apply, invalidated=_identities(ctx) if channels else set())


def _iq_rx(ctx, options):
    _validate(options, ("frequency_hz", "sample_rate_hz", "rx_gain", "gain_mode", "output", "capture_path", "bandwidth_mhz"))
    settings, receivers, invalidated = {}, {}, set()
    receiver = {}
    if "frequency_hz" in options:
        receiver["rf_freq_hz"] = options["frequency_hz"]
    if "rx_gain" in options:
        receiver.update(rx_gain=options["rx_gain"], gain_mode=options.get("gain_mode", "manual"))
    elif "gain_mode" in options:
        receiver["gain_mode"] = options["gain_mode"]
    if receiver:
        receivers = {a: dict(antid=a, **receiver) for a in ctx.antennas}
    tx = _rpc(ctx.board, "get_iq_tx") if any(ctx.before[a] == "iq_tx" for a in ctx.antennas) else None
    for a in ctx.antennas:
        old = tx["sensors"][a]["frequency_hz"] if ctx.before[a] == "iq_tx" else ctx.snapshot["receivers"][a]["rf_freq_hz"]
        if ctx.before[a] == "iq_tx" and "frequency_hz" not in options:
            receivers.setdefault(a, {"antid": a})["rf_freq_hz"] = old
        if ((ctx.snapshot.get("capture_path") == "bluetooth" or ctx.snapshot.get("trigger", {}).get("mode") == "downsample")
                and gain_changed(ctx.snapshot["receivers"][a], receiver)):
            invalidated.add(Antenna(ctx.board.host, a))
        if "frequency_hz" in options and options["frequency_hz"] != old:
            invalidated.add(Antenna(ctx.board.host, a))
    if receivers:
        settings["receivers"] = list(receivers.values())
    path = options.get("capture_path", ctx.snapshot.get("capture_path", "wifi"))
    bluetooth_rates = (32e6, 16e6, 8e6, 4e6, 3.2e6, 1.6e6)
    rate = options.get("sample_rate_hz")
    if rate in IQ_RX_STREAM_RATES and "capture_path" not in options:
        path = "bluetooth"
    if "capture_path" not in options and rate not in IQ_RX_STREAM_RATES and rate is not None:
        candidates = [p for p, rates in (("wifi", DECIM_TO_FS.values()), ("bluetooth", bluetooth_rates)) if rate in rates]
        if len(candidates) == 1 and path != candidates[0]:
            path = settings["capture_path"] = candidates[0]
            invalidated.update(_identities(ctx))
    if path not in ("wifi", "bluetooth"):
        raise ValueError("capture_path must be wifi or bluetooth")
    if options.keys() & {"capture_path", "bandwidth_mhz"}:
        if options.get("sample_rate_hz") in IQ_RX_STREAM_RATES and (
                options.get("capture_path", "bluetooth") != "bluetooth" or options.get("bandwidth_mhz", 1.2) != 1.2):
            raise ValueError("Continuous downsampling requires Bluetooth with the 1.2 MHz filter")
        if ctx.snapshot.get("trigger", {}).get("mode") == "downsample":
            settings["trigger"] = {"mode": "interval"}
    if "capture_path" in options:
        _board_wide(ctx)
        settings["capture_path"] = path
        if path != ctx.snapshot.get("capture_path", "wifi"):
            invalidated.update(_identities(ctx))
    if "bandwidth_mhz" in options:
        _board_wide(ctx)
        bw = options["bandwidth_mhz"]
        if path == "bluetooth":
            if bw not in (1.2, 1.5, 2.1, 2.9, 3.8, 5.2, 8.3):
                raise ValueError("Unsupported Bluetooth filter bandwidth")
            settings["bluetooth_filter_khz"] = round(bw * 1000)
        else:
            if bw != 0 and (bw != int(bw) or not 13 <= bw <= 54):
                raise ValueError("WiFi filter bandwidth must be 0 or 13 to 54 MHz")
            settings["filter_bw_mhz"] = int(bw)
        invalidated.update(_identities(ctx))
    if "sample_rate_hz" in options:
        _board_wide(ctx)
        rates = {v: k for k, v in DECIM_TO_FS.items()}
        rate = options["sample_rate_hz"]
        if rate in IQ_RX_STREAM_RATES:
            settings.update(capture_path="bluetooth", bluetooth_sample_rate_hz=1600000, bluetooth_filter_khz=1200)
            settings["trigger"] = {"mode": "downsample", "sample_rate_hz": int(rate)}
        elif path == "bluetooth" and rate in bluetooth_rates:
            settings["bluetooth_sample_rate_hz"] = int(rate)
            if ctx.snapshot.get("trigger", {}).get("mode") == "downsample":
                settings["trigger"] = {"mode": "interval"}
        elif path == "wifi" and rate in rates:
            settings["adc_decimation"] = rates[rate]
            if ctx.snapshot.get("trigger", {}).get("mode") == "downsample":
                settings["trigger"] = {"mode": "interval"}
        else:
            raise ValueError(f"Unsupported {path} IQ receive sample rate {rate}")
        if rate != receive_sample_rate(ctx.snapshot):
            invalidated.update(_identities(ctx))
    if "output" in options:
        _board_wide(ctx)
        settings["rf_switch"] = _ROUTES[options["output"]]
    return ConfigurationUpdate(iq_settings=settings, invalidated=invalidated)


def _iq_tx(ctx, options):
    _validate(options, ("frequency_hz", "sample_rate_hz", "rf_gain", "bandwidth_mhz", "output"))
    if options.get("output") == "random":
        raise ValueError("Random RF input is a receiver setting")
    transmitters, invalidated = [], set()
    common = {}
    tx = _rpc(ctx.board, "get_iq_tx") if options.keys() & {"frequency_hz", "sample_rate_hz"} else None
    if "sample_rate_hz" in options:
        if any(mode == "iq_tx" and a not in ctx.antennas for a, mode in enumerate(ctx.modes)):
            raise ValueError("Transmit sample rate is board-wide; select every IQ TX sensor on the board")
        if options["sample_rate_hz"] not in (80000000, 40000000, 32000000, 16000000):
            raise ValueError("Unsupported transmit sample rate")
        common = {"sample_rate_hz": options["sample_rate_hz"]}
        if options["sample_rate_hz"] != tx.get("sample_rate_hz", 80000000):
            common["muted"] = True
            invalidated.update(_identities(ctx))
    for a in ctx.antennas:
        settings = {k: v for k, v in options.items() if k != "sample_rate_hz"}
        if ctx.before[a] != "iq_tx":
            settings["amplitude"] = 0
            if ctx.before[a] == "iq_rx" and "frequency_hz" not in options:
                settings["frequency_hz"] = ctx.snapshot["receivers"][a]["rf_freq_hz"]
        if "frequency_hz" in options:
            old = tx["sensors"][a]["frequency_hz"] if ctx.before[a] == "iq_tx" else ctx.snapshot["receivers"][a]["rf_freq_hz"]
            if old != options["frequency_hz"]:
                invalidated.add(Antenna(ctx.board.host, a))
        if settings:
            transmitters.append(dict(antid=a, **settings))
    payload = {**common, **({"transmitters": transmitters} if transmitters else {})}
    return ConfigurationUpdate(
        before_modes=(lambda: _rpc(ctx.board, "set_iq_tx", payload)) if payload else None,
        invalidated=invalidated,
    )


for _mode, _planner in {"ble": _ble, "wifi": _wifi, "wifi_iq": _wifi_iq, "iq_rx": _iq_rx, "iq_tx": _iq_tx}.items():
    register_configuration(_mode, _planner)


def configure(selection, *, mode=None, **options):
    options = {k: v for k, v in options.items() if v is not None}
    if "frequency_hz" in options and (not math.isfinite(options["frequency_hz"]) or options["frequency_hz"] <= 0):
        raise ValueError("frequency_hz must be positive and finite")
    if options.get("output") == "isolation":
        options["output"] = "isolated"
    if "output" in options and options["output"] not in _ROUTES:
        raise ValueError(f"output must be one of {tuple(_ROUTES)}")
    with _LOCK:
        planners = dict(_PLANNERS)
    if mode is not None and mode not in planners:
        raise ValueError(f"No configuration planner registered for {mode!r}")
    owner = selection._pool
    with owner._lock:
        owner.open()
        plans, invalidated, switched = [], set(), False
        for bi, board in enumerate(owner.boards):
            ids = tuple(a.antid for a in selection.antennas if a.host == board.host)
            if not ids:
                continue
            snapshot = board.iq.get_config()
            before = tuple(snapshot["modes"])
            modes = tuple(mode if mode is not None and a in ids else before[a] for a in range(8))
            updates = []
            iq = {"modes": list(modes)} if mode is not None else {}
            for role in dict.fromkeys(modes[a] for a in ids):
                if role not in planners:
                    raise ValueError(f"No configuration planner registered for {role!r}")
                ctx = ConfigurationContext(owner, board, bi, tuple(a for a in ids if modes[a] == role), before, modes, snapshot)
                update = planners[role](ctx, dict(options))
                if not isinstance(update, ConfigurationUpdate):
                    raise TypeError("Configuration planner must return ConfigurationUpdate")
                if iq.keys() & update.iq_settings.keys():
                    raise ValueError("Configuration planners produced conflicting shared settings")
                iq.update(update.iq_settings)
                invalidated.update(update.invalidated)
                updates.append(update)
            for a in ids:
                if before[a] != modes[a]:
                    if before[a] in ("iq_rx", "iq_tx") and modes[a] in ("iq_rx", "iq_tx"):
                        switched = True
                    else:
                        invalidated.add(Antenna(board.host, a))
            plans.append((bi, board, before, modes, iq, updates))
        # No writes occur until every board and mode planner has validated.
        owner.calibrations = owner.calibrations.without(invalidated)
        iq_receiver = owner._receivers.get("iq")
        if iq_receiver is not None and invalidated:
            iq_receiver._invalidate_stream_phases(invalidated)
        try:
            for bi, board, before, modes, iq, updates in plans:
                for update in updates:
                    if update.before_modes:
                        update.before_modes()
                if iq:
                    board.iq.set_config(iq)
                if mode is not None:
                    _wait(board, lambda s, modes=modes: all(x["applied_mode"] == modes[a] for a, x in enumerate(s["sensors"])), 10)
                for update in updates:
                    if update.after_modes:
                        update.after_modes()
        except Exception:
            owner.calibrations = owner.calibrations.without(selection.antennas)
            for _, board, *_ in plans:
                try:
                    _rpc(board, "set_iq_tx", {"muted": True})
                except Exception:
                    pass
            raise
        finally:
            for bi, board, before, modes, _, _ in plans:
                if before != modes:
                    for receiver in tuple(owner._receivers.values()):
                        receiver.modes_changed(bi, before, modes)
            if mode is not None:
                owner._refresh_receivers()
            if owner._tx is not None and not owner._tx.closed:
                owner._tx.read()
        if switched and owner.calibrations.groups:
            owner.refresh_calibration()
    return selection
