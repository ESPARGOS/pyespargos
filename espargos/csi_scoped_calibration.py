"""WiFi calibration for antenna subsets, grouped by their actual RX channels."""

from contextlib import ExitStack
import time
import numpy as np

from .calibration import Antenna, AntennaCorrection, Calibration, CalibrationGroup, CalibrationState
from . import csi_processing, sensor
from .sensor_calibration import compute_reference_path_delays


def channels(config, overrides):
    """Return primary/secondary pairs in hardware antenna-ID order."""
    return [(int(overrides["channel-primary"][a]), int(overrides["channel-secondary"][a])) if overrides["override_active"] else (int(config["channel-primary"]), int(config["channel-secondary"])) for a in range(8)]


def states(pool):
    """Read current WiFi settings without reconciling or changing boards.

    WiFi CSI has no hardware calibration epoch. The epoch here is pool-local:
    callers must invalidate after changes made outside this pool, even when
    restoring the same channel. Boot IDs and observed config changes are checked.
    """
    result = {}
    for b in pool.boards:
        pairs = channels(b.wifi_rx.get_config(), b.wifi_rx.get_channel_overrides())
        status = b.control.get_json("get_iq_tx")
        for a, (primary, secondary) in enumerate(pairs):
            s = status["sensors"][a]
            if s["applied_mode"] != "wifi":
                continue
            relative = -1 if secondary == 2 else secondary
            center = csi_processing.get_center_frequency(primary, primary + 4 * relative)
            state = CalibrationState(
                Antenna(b.host, a), "rx", center, 40e6 if relative else 20e6, s["boot_id"], pool._wifi_calibration_epochs[Antenna(b.host, a)], (("mode", "wifi"), ("primary", primary), ("secondary", relative), ("gain_phase", pool._gain_phase_enabled))
            )
            result[state.antenna] = state
    fingerprint = {a: (s.frequency_hz, s.boot_id, s.settings) for a, s in result.items()}
    previous = getattr(pool, "_wifi_calibration_fingerprint", None)
    pool._wifi_calibration_fingerprint = fingerprint
    if previous is not None and previous != fingerprint:
        changed = {a for a in previous.keys() | fingerprint.keys() if previous.get(a) != fingerprint.get(a)}
        pool._invalidate_association_timebase("WiFi sensor configuration changed", antennas=changed)
        from dataclasses import replace

        result = {a: replace(s, epoch=pool._wifi_calibration_epochs[a]) for a, s in result.items()}
    return result


def frequencies(state, csi_format):
    settings = dict(state.settings)
    if state.direction != "rx" or settings.get("mode") != "wifi":
        raise ValueError("A WiFi RX calibration state is required")
    primary, secondary = settings["primary"], settings["secondary"]
    if csi_format == "ht40":
        if not secondary:
            raise ValueError("HT40 requires a bonded channel")
        return csi_processing.get_frequencies_ht40(primary, primary + 4 * secondary)
    if csi_format == "he20" and secondary:
        raise ValueError("HE20 requires a 20 MHz receiver configuration")
    functions = {"lltf": csi_processing.get_frequencies_lltf, "ht20": csi_processing.get_frequencies_ht20, "he20": csi_processing.get_frequencies_he20}
    if csi_format not in functions:
        raise ValueError(f"Unknown CSI format {csi_format!r}")
    return functions[csi_format](primary)


def apply_csi(calibration, state, values, csi_format="lltf"):
    """Correct one antenna's deserialized CSI using an explicitly checked state.

    Leading batch dimensions are allowed. Values must already include the same
    timestamp and gain-phase corrections used by CSICluster deserialization.
    Unmeasured/stale states raise KeyError rather than applying identity gains.
    """
    grid = frequencies(state, csi_format)
    values = np.asarray(values)
    if values.ndim == 0 or values.shape[-1] != len(grid):
        raise ValueError("CSI length does not match its preamble format")
    return values * calibration.correction(state).response(grid)


def apply_cluster(pool, cluster, csi_format, calibration, current):
    formats = {"lltf": ("has_lltf", "deserialize_csi_lltf"), "ht20": ("has_ht20ltf", "deserialize_csi_ht20ltf"), "ht40": ("has_ht40ltf", "deserialize_csi_ht40ltf"), "he20": ("has_he20ltf", "deserialize_csi_he20ltf")}
    if csi_format not in formats:
        raise ValueError(f"Unknown CSI format {csi_format!r}")
    available, decode = formats[csi_format]
    if not getattr(cluster, available):
        raise ValueError(f"Cluster does not contain {csi_format} CSI")
    selected = set()
    for group in calibration.groups:
        for correction in group.corrections:
            cfg = dict(correction.state.settings)
            if cfg.get("mode") == "wifi" and (cfg["primary"], cfg["secondary"]) == (cluster.primary_channel, cluster.secondary_channel_relative):
                selected.add(correction.state.antenna)
    if not selected:
        raise KeyError("No WiFi calibration for this channel")
    raw = getattr(cluster, decode)()
    result = np.full(raw.shape, np.nan + 1j * np.nan)
    for bi, b in enumerate(pool.boards):
        for a in range(8):
            antenna = Antenna(b.host, a)
            pos = (bi, *b.revision.antenna_id_to_row_col(a))
            if antenna not in selected or not cluster.completion[pos]:
                continue
            state = current[antenna]
            cfg = dict(state.settings)
            if (cfg.get("primary"), cfg.get("secondary")) != (cluster.primary_channel, cluster.secondary_channel_relative):
                raise KeyError(f"{antenna}: packet channel does not match current state")
            result[pos] = apply_csi(calibration, state, raw[pos], csi_format)
    return result


def estimate(clusters, positions, primary, secondary, minimum=5):
    """Estimate only the selected antennas, never require unrelated sensors."""
    formats = [("ht40", "has_ht40ltf", "deserialize_csi_ht40ltf"), ("ht20", "has_ht20ltf", "deserialize_csi_ht20ltf"), ("lltf", "has_lltf", "deserialize_csi_lltf")]
    matching = [c for c in clusters if c.primary_channel == primary and c.secondary_channel_relative == secondary and all(c.completion[p] for p in positions)]
    for name, available, deserialize in formats:
        usable = [c for c in matching if getattr(c, available)]
        if len(usable) < minimum:
            continue
        decoded = [getattr(c, deserialize)() for c in usable]
        values = np.array([[v[p] for p in positions] for v in decoded])[:, None, None]
        timestamps = np.array([[c.sensor_timestamps[p] for p in positions] for c in usable])[:, None, None]
        good = np.isfinite(values).all(axis=(1, 2, 3, 4)) & (np.max(abs(values), axis=-1) > 0).all(axis=(1, 2, 3))
        if good.sum() < minimum:
            continue
        values, timestamps = values[good], timestamps[good]
        grid, valid = csi_processing.get_csi_sto_correction_frequencies(name, secondary)
        delay, phase = csi_processing.estimate_phase_time_offsets(values, timestamps, grid, valid)
        return delay.ravel(), np.angle(phase.ravel())
    from .wifi_transceiver import CalibrationError

    raise CalibrationError(f"Channel {primary}/{secondary}: need {minimum} complete reference packets for the selected {len(positions)} antennas; received {len(matching)} matching packets")


def calibrate(pool, *, antennas=None, per_board=True, duration=2, cable_lengths=None, cable_velocity_factors=None, run_in_thread=True):
    """Measure the selected WiFi antennas on their existing channels.

    One reference capture per channel, without retuning receivers. ``duration``
    is per channel. Different channels and independent boards remain separate
    phase-reference groups. All reference/filter settings are restored on failure.
    """
    if not np.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be positive and finite")
    if per_board and (cable_lengths is not None or cable_velocity_factors is not None):
        raise ValueError("Cable compensation requires per_board=False")
    current = states(pool)
    selected = list(current) if antennas is None else list(antennas)
    if not selected or len(set(selected)) != len(selected) or any(a not in current for a in selected):
        raise ValueError("Select distinct antennas currently in WiFi mode")
    pool._invalidate_association_timebase("new WiFi calibration started", warn=False, invalidate_scoped=False)
    current = states(pool)
    groups = {}
    for a in selected:
        cfg = dict(current[a].settings)
        key = (cfg["primary"], cfg["secondary"], a.host if per_board else None)
        groups.setdefault(key, []).append(a)
    indices = {b.host: i for i, b in enumerate(pool.boards)}
    positions = {a: (indices[a.host], *pool.boards[indices[a.host]].revision.antenna_id_to_row_col(a.antid)) for a in selected}
    paths = compute_reference_path_delays(pool.boards, cable_lengths, cable_velocity_factors)
    measured = []
    boards = list(dict.fromkeys(pool.boards + pool._reference_generator_boards))
    with ExitStack() as restore:
        for b in boards:
            config = b.wifi_rx.get_config()
            restore.callback(b.wifi_rx.set_reference_config, {"channel-primary": config["channel-primary"], "channel-secondary": config["channel-secondary"]})
            restore.callback(b.wifi_rx.set_rf_switch, b.wifi_rx.get_rf_switch())
            restore.callback(b.wifi_rx.set_mac_filter, b.wifi_rx.get_mac_filter())
            b.wifi_rx.clear_mac_filter()
        for primary, secondary in dict.fromkeys((p, s) for p, s, _ in groups):
            for b in boards:
                b.wifi_rx.set_rf_switch(sensor.RFSwitchState.SENSOR_RFSWITCH_ISOLATION)
                b.wifi_rx.set_reference_config({"channel-primary": primary, "channel-secondary": 2 if secondary == -1 else secondary})
            if run_in_thread:
                # Process the old channel's backlog before starting the capture
                # timer. A slow consumer must still get a full fresh capture.
                pool._run(timeout=0)
            pool._clear_cluster_cache("calibration")
            for b in boards:
                b.wifi_rx.set_rf_switch(sensor.RFSwitchState.SENSOR_RFSWITCH_REFERENCE)
            start = time.monotonic()
            while time.monotonic() - start < duration:
                if run_in_thread:
                    pool._run(timeout=min(0.05, max(0, duration - (time.monotonic() - start))))
                else:
                    time.sleep(0.01)
            for b in boards:
                b.wifi_rx.set_rf_switch(sensor.RFSwitchState.SENSOR_RFSWITCH_ISOLATION)
            clusters = pool._get_cluster_cache_snapshot("calibration")
            for (p, s, host), members in groups.items():
                if (p, s) != (primary, secondary):
                    continue
                delay, phase = estimate(clusters, [positions[a] for a in members], p, s)
                corrections = []
                for a, d, phi in zip(members, delay, phase):
                    path = paths[positions[a]] - paths[positions[members[0]]]
                    state = current[a]
                    corrections.append(AntennaCorrection(state, -phi - 2 * np.pi * state.frequency_hz * path, path - d))
                measured.append(CalibrationGroup(tuple(corrections)))
    pool._clear_cluster_cache("ota")
    after = states(pool)
    if any(after.get(a) != current[a] for a in selected):
        from .wifi_transceiver import CalibrationError

        changed = [f"{a.host}/{a.antid}" for a in selected if after.get(a) != current[a]]
        raise CalibrationError(f"WiFi configuration or clock changed during calibration: {', '.join(changed)}")
    result = Calibration(tuple(measured), {"method": "wifi-reference"})
    pool._owner.calibrations = pool._owner.calibrations.merge(result)
    return result
