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
from .iq_packet import DECIM_TO_FS
from .iq_tx_control import _rpc, _wait

_ROUTES = {"isolation": 0, "reference": 1, "antenna_r": 2, "antenna_l": 3, "random": 4}
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


def _wifi(ctx, options):
    _validate(options, ("frequency_hz", "channel", "secondary_channel", "output"))
    channels = None
    if any(k in options for k in ("frequency_hz", "channel", "secondary_channel")):
        primary = options.get("channel")
        if "frequency_hz" in options:
            found = next((c for c in range(1, 15) if (2484e6 if c == 14 else (2407 + 5 * c) * 1e6) == options["frequency_hz"]), None)
            if found is None or primary is not None and primary != found:
                raise ValueError("WiFi frequency must match the primary channel center")
            primary = found
        if primary is not None and (type(primary) is not int or not 1 <= primary <= 14):
            raise ValueError("WiFi channel must be 1–14")
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
    _validate(options, ("frequency_hz", "sample_rate_hz", "rx_gain", "gain_mode", "output"))
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
        if "frequency_hz" in options and options["frequency_hz"] != old:
            invalidated.add(Antenna(ctx.board.host, a))
    if receivers:
        settings["receivers"] = list(receivers.values())
    if "sample_rate_hz" in options:
        _board_wide(ctx)
        rates = {v: k for k, v in DECIM_TO_FS.items()}
        if options["sample_rate_hz"] not in rates:
            raise ValueError(f"sample_rate_hz must be one of {tuple(rates)}")
        settings["adc_decimation"] = rates[options["sample_rate_hz"]]
        if settings["adc_decimation"] != ctx.snapshot["adc_decimation"]:
            invalidated.update(_identities(ctx))
    if "output" in options:
        _board_wide(ctx)
        settings["rf_switch"] = _ROUTES[options["output"]]
    return ConfigurationUpdate(iq_settings=settings, invalidated=invalidated)


def _iq_tx(ctx, options):
    _validate(options, ("frequency_hz", "rf_gain", "bandwidth_mhz", "output"))
    if options.get("output") == "random":
        raise ValueError("Random RF input is a receiver setting")
    transmitters, invalidated = [], set()
    tx = _rpc(ctx.board, "get_iq_tx") if "frequency_hz" in options else None
    for a in ctx.antennas:
        settings = dict(options)
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
    return ConfigurationUpdate(before_modes=(lambda: _rpc(ctx.board, "set_iq_tx", {"transmitters": transmitters})) if transmitters else None, invalidated=invalidated)


for _mode, _planner in {"ble": _ble, "wifi": _wifi, "iq_rx": _iq_rx, "iq_tx": _iq_tx}.items():
    register_configuration(_mode, _planner)


def configure(selection, *, mode=None, **options):
    options = {k: v for k, v in options.items() if v is not None}
    if "frequency_hz" in options and (not math.isfinite(options["frequency_hz"]) or options["frequency_hz"] <= 0):
        raise ValueError("frequency_hz must be positive and finite")
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
