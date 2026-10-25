"""TX calibration using the master controller's reference monitor.

After RX synchronization, sensors transmit pilots through REF to the master
controller's monitor. Relative pilot phases and delays, corrected for known
reference-path delays, determine the TX phase and delay corrections.

Unlike ``reciprocity``, this method measures all transmitters
at one common receiver instead of exchanging pilots between sensors. It needs
a reverse RF path from every sensor to the master; amplified splitters without
that path are not supported. Reciprocity instead cancels the propagation
response using two-way sensor links and also supports antenna paths.
"""

import copy
import numpy as np
from ..iq_tx_control import TXStatus, _rpc, _waveform
from .pilots import pilots
from .monitor_fit import measure
from ..sensor_calibration import compute_reference_path_delays
from .reference import _map


def calibrate(pool, *, rx_gain=None, cable_lengths=None, cable_velocity_factors=None, timeout=10.0, scope=None):
    """Synchronize RX, measure TX through REF, and restore user waveforms.

    All selected transmitters use one frequency and replay length. Combined
    arrays need at least two transmitters on one board to link pilot groups.
    Failures leave TX muted. The monitor's saved configuration is restored.
    """
    from ..reference_monitor import ReferenceMonitor

    if pool._calibrating:
        raise RuntimeError("Calibration is already running")
    from .scope import CalibrationScope

    scope = scope or CalibrationScope(pool)
    boards = pool.boards
    saved_rx = _map(lambda b: b.iq.get_config(), boards)
    saved_tx = _map(lambda b: _rpc(b, "get_iq_tx"), boards)
    targets = [[a for a in scope.ids[bi] if c["modes"][a] == "iq_tx"] for bi, c in enumerate(saved_rx)]
    selected = [(bi, a) for bi, ants in enumerate(targets) for a in ants]
    if not selected:
        raise ValueError("Select at least one IQ TX sensor")
    sources = [saved_tx[bi]["sensors"][a] for bi, a in selected]
    length, center = sources[0]["samples"], sources[0]["frequency_hz"]
    if not length or any(s["samples"] != length or s["frequency_hz"] != center for s in sources):
        raise ValueError("Prepare transmitters with one common frequency and sample count")
    active = [bi for bi, ants in enumerate(targets) if ants]
    base = next((bi for bi in active if len(targets[bi]) >= 2), active[0])
    if len(active) > 1 and len(targets[base]) < 2:
        raise ValueError("Combined calibration needs at least two TX sensors on one board")
    if any(r["rf_freq_hz"] != center for bi, c in enumerate(saved_rx) for a, r in enumerate(c["receivers"]) if a in scope.ids[bi] and c["modes"][a] == "iq_rx"):
        raise ValueError("Active receivers must use the TX frequency")
    paths = compute_reference_path_delays(boards, cable_lengths, cable_velocity_factors)

    def path(sensor):
        bi, a = sensor
        return paths[(bi, *boards[bi].revision.antenna_id_to_row_col(a))]

    steering = [copy.deepcopy(s["steering"]) for s in saved_tx]
    for bi, a in scope.pairs:
        for values in steering[bi].values():
            values[a] = 0
    corrections = [copy.deepcopy(s["calibration"]) for s in saved_tx]
    monitors = _map(lambda b: _rpc(b, "get_iq_monitor"), boards)
    status = TXStatus(boards)
    monitor = ReferenceMonitor(pool.master_board)
    prepared = set()
    success = False
    phase_error = delay_error = 0.0
    epochs = {}

    def wait_modes(bi, modes):
        return status.wait(boards[bi], lambda s: all(s["sensors"][a]["applied_mode"] == modes[a] for a in scope.ids[bi]), timeout)

    def weights(bi):
        cal = corrections[bi]
        return status.wait(
            boards[bi],
            lambda s: all(s["sensors"][a]["enabled"] and abs(s["sensors"][a]["phase_deg"] - cal["phase_deg"][a]) < 0.01 and abs(s["sensors"][a]["delay_samples"] - cal["delay_samples"][a]) < 0.001 for a in targets[bi]),
            timeout,
        )

    try:

        def prepare(bi):
            b = boards[bi]
            _rpc(b, "set_iq_monitor", {"enabled": False})
            # The pilots span ±30 MHz, regardless of the user's waveform.
            # A narrower TX filter bends their phase enough to hide the origin.
            modes = scope.modes(bi, saved_rx[bi]["modes"])
            _rpc(b, "set_iq_tx", {"muted": True, "modes": modes, "transmitters": [{"antid": a, "rf_gain": {"a": 0, "b": 0}, "bandwidth_mhz": 60} for a in targets[bi]]})
            wait_modes(bi, modes)

        _map(prepare, scope.active)
        scope.apply_rx({"rf_freq_hz": center})
        rx_cal = pool._sync(center_hz=center, rx_gain=rx_gain, cable_lengths=cable_lengths, cable_velocity_factors=cable_velocity_factors, scope=scope, publish=False)
        pool._calibrating = True
        for bi in active:
            b = boards[bi]
            for a in targets[bi]:
                corrections[bi]["phase_deg"][a] = corrections[bi]["delay_samples"][a] = 0
            _waveform(b, {"type": "calibration", "samples": length, "amplitude": 192 / 511, "antennas": targets[bi]}, timeout, wait=status.wait)
            prepared.add(bi)
            _rpc(
                b,
                "set_iq_tx",
                {
                    "muted": True,
                    "modes": saved_rx[bi]["modes"],
                    "calibration": corrections[bi],
                    "steering": steering[bi],
                    "transmitters": [{"antid": a, "output": "reference", "amplitude": 1} for a in targets[bi]] + [{"antid": a, "amplitude": 0} for a, m in enumerate(saved_rx[bi]["modes"]) if m == "iq_tx" and a not in targets[bi]],
                },
            )
            wait_modes(bi, saved_rx[bi]["modes"])
            state = weights(bi)
            for a in targets[bi]:
                if state["sensors"][a]["boot_id"] != saved_tx[bi]["sensors"][a]["boot_id"]:
                    raise RuntimeError("A transmitter rebooted during calibration")
                epochs[bi, a] = state["sensors"][a]["epoch"]
        _rpc(
            pool.master_board,
            "set_iq_monitor",
            {
                "enabled": True,
                "frequency_hz": center,
                "sample_rate_hz": 80000000,
                "bandwidth_mhz": 60,
                "gain_mode": "manual",
                # Resolve the wideband pilots above monitor quantization noise.
                "gain": 12,
                "samples": 4096,
                "interval_us": 20000,
                "offset_us": 0,
            },
        )
        models = pilots(min(length, 1024))
        anchor = (base, targets[base][0])
        groups = [([(base, a, slot) for slot, a in enumerate(targets[base])], anchor, set((base, a) for a in targets[base]))]
        for bi in active:
            if bi == base:
                continue
            if len(targets[bi]) > 1:
                group = [(*anchor, 0)] + [(bi, a, slot) for slot, a in enumerate(targets[bi]) if slot]
                groups.append((group, anchor, set((bi, a) for a in targets[bi][1:])))
            groups.append(([(bi, targets[bi][0], 0), (base, targets[base][1], 1)], (base, targets[base][1]), {(bi, targets[bi][0])}))
        for group, reference, adjust in groups:
            emitting = {(bi, a) for bi, a, _ in group}
            for bi in active:
                _rpc(boards[bi], "set_iq_tx", {"muted": False, "transmitters": [{"antid": a, "amplitude": int((bi, a) in emitting)} for a in targets[bi]]})
                status.wait(boards[bi], lambda s, bi=bi: all(abs(s["sensors"][a]["applied_amplitude"] - int((bi, a) in emitting)) < 1e-6 for a in targets[bi]), timeout)
            # Dense pilots need enough ADC resolution per tone. Set gain from
            # the received peak, leaving headroom when several TXs add together.
            monitor_config = _rpc(pool.master_board, "get_iq_monitor")
            gain = monitor_config["gain"]
            for _ in range(6):
                captures = monitor.collect(3, timeout)
                peak = float(np.median([max(abs(z.real).max(), abs(z.imag).max()) for z in captures]))
                if 64 <= peak <= 400:
                    break
                adjusted = int(np.clip(gain + (6 if peak < 64 else -6), 0, monitor_config["gain_max"]))
                if adjusted == gain:
                    break
                gain = adjusted
                _rpc(pool.master_board, "set_iq_monitor", {"gain": gain})
            index = next(j for j, (bi, a, _) in enumerate(group) if (bi, a) == reference)
            delays = np.array([path((bi, a)) for bi, a, _ in group])
            delays -= delays[index]
            for iteration in range(3):
                phase, delay = measure(monitor, [models[slot] for _, _, slot in group], timeout)
                phase = np.angle(np.exp(1j * (phase - phase[index] + 2 * np.pi * center * delays)))
                delay = delay - delay[index] - 80e6 * delays
                indices = [j for j, (bi, a, _) in enumerate(group) if (bi, a) in adjust]
                pe = float(np.max(abs(np.rad2deg(phase[indices]))))
                de = float(np.max(abs(delay[indices])))
                if iteration and pe <= 3 and de <= 0.05:
                    break
                if iteration == 2:
                    if pe > 10 or de > 0.3:
                        raise RuntimeError(f"Controller-monitor calibration did not validate: {pe:.1f} degrees, {de:.3f} samples")
                    break
                for j, (bi, a, _) in enumerate(group):
                    if (bi, a) in adjust:
                        corrections[bi]["phase_deg"][a] -= float(np.rad2deg(phase[j]))
                        corrections[bi]["delay_samples"][a] -= float(delay[j])
                for bi in {bi for bi, a in adjust}:
                    _rpc(boards[bi], "set_iq_tx", {"calibration": corrections[bi]})
                    weights(bi)
            phase_error = max(phase_error, pe)
            delay_error = max(delay_error, de)
        for bi in active:
            current = _rpc(boards[bi], "get_iq_tx")
            for a in targets[bi]:
                sensor = current["sensors"][a]
                if sensor["boot_id"] != saved_tx[bi]["sensors"][a]["boot_id"] or sensor["epoch"] != epochs[bi, a]:
                    raise RuntimeError("A transmitter restarted during calibration")
        success = True
        pool._calibration = rx_cal
        return {
            "calibration": corrections,
            "phase_residual_deg": phase_error,
            "delay_residual_samples": delay_error,
            "groups": len(groups),
            "_center_hz": center,
            "_tx_states": {(bi, a): (saved_tx[bi]["sensors"][a]["boot_id"], epochs[bi, a]) for bi, a in selected},
        }
    finally:
        errors = []
        try:
            _rpc(pool.master_board, "set_iq_monitor", {"enabled": False})
        except Exception as error:
            errors.append(str(error))
        for bi in scope.active:
            b = boards[bi]
            try:
                _rpc(b, "set_iq_tx", {"muted": True})
                status.wait(b, lambda s: all(not x["enabled"] or abs(x["applied_amplitude"]) < 1e-8 for x in s["sensors"]), timeout)
                if bi in prepared:
                    _waveform(b, {"type": "restore", "samples": length, "antennas": targets[bi]}, timeout, wait=status.wait)
                _rpc(
                    b,
                    "set_iq_tx",
                    {
                        "muted": True,
                        "calibration": corrections[bi] if success else saved_tx[bi]["calibration"],
                        "steering": saved_tx[bi]["steering"],
                        "transmitters": [{k: x[k] for k in ("antid", "frequency_hz", "rf_gain", "bandwidth_mhz", "output", "amplitude")} for x in saved_tx[bi]["sensors"]],
                    },
                )
                b.iq.set_config({**{k: saved_rx[bi][k] for k in ("modes", "rf_switch", "adc_decimation", "trigger")}, "receivers": [saved_rx[bi]["receivers"][a] for a in scope.ids[bi]]})
                if success and targets[bi]:
                    restored = _rpc(b, "get_iq_tx")
                    if any(restored["sensors"][a]["epoch"] != epochs[bi, a] or restored["sensors"][a]["boot_id"] != saved_tx[bi]["sensors"][a]["boot_id"] for a in targets[bi]):
                        raise RuntimeError("Restoring settings restarted a calibrated transmitter")
            except Exception as error:
                errors.append(f"{b.host}: {error}")
        try:
            pool.master_board.iq.set_reference()
        except Exception as error:
            errors.append(str(error))
        for bi in scope.active:
            b, c = boards[bi], monitors[bi]
            try:
                _rpc(
                    b,
                    "set_iq_monitor",
                    {k: c[k] for k in ("enabled", "gain_mode", "frequency_hz", "sample_rate_hz", "gain", "bandwidth_mhz", "samples", "interval_us", "offset_us")},
                )
            except Exception as error:
                errors.append(f"{b.host}: {error}")
        status.close()
        monitor.close()
        pool._calibrating = False
        if not success or errors:
            pool._calibration = None
        if success and not errors:
            try:
                _map(lambda pair: _rpc(pair[1], "set_iq_tx", {"muted": saved_tx[pair[0]]["muted"]}), [(bi, boards[bi]) for bi in scope.active])
            except Exception as error:
                errors.append(str(error))
                for b in [boards[bi] for bi in scope.active]:
                    try:
                        _rpc(b, "set_iq_tx", {"muted": True})
                    except Exception as mute_error:
                        errors.append(str(mute_error))
        if errors:
            raise RuntimeError("Could not restore calibration settings: " + "; ".join(errors))
