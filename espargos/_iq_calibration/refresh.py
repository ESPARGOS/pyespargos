"""Transfer existing IQ corrections after a same-frequency RX/TX mode switch.

The pool reads the new sample origins and adjusts stored delays without
remeasuring RF phase. This requires a previously measured correction for each
direction; retuning or rebooting invalidates it. Capture settings are restored
before returning. Radar timing references remain separate from this transfer.
"""

import numpy as np

from ..calibration import Antenna, CalibrationState
from .capture import _Captures
from ..iq_tx_control import _rpc
from ..iq_packet import IQ_SYNC_INFO_GRID_SYNCED, IQ_SYNC_INFO_FIRE_TIME_VALID


def refresh(owner, timeout):
    """Read sample start times and update the stored RX/TX corrections."""
    boards = owner.boards
    configs = [b.iq.get_config() for b in boards]
    before = [_rpc(b, "get_iq_tx") for b in boards]
    calibration = owner.calibrations
    known = {(c.state.antenna, c.state.direction) for g in calibration.groups for c in g.corrections}
    receivers = [(bi, a) for bi, (b, config) in enumerate(zip(boards, configs)) for a, mode in enumerate(config["modes"]) if mode == "iq_rx" and (Antenna(b.host, a), "rx") in known]
    states = []
    capture = _Captures(boards)
    changed = []
    try:
        generations = {}
        for bi in sorted({bi for bi, _ in receivers}):
            changed.append(bi)
            boards[bi].iq.set_config({"trigger": {"mode": "interval", "interval_chunks": 2048, "burst_chunks": 1}})
            generation = boards[bi].iq.get_config()["config_generation"]
            generations.update({8 * bi + a: generation for j, a in receivers if j == bi})
        packets = capture.collect(list(generations), 256, 1, timeout, generations)
        after = [_rpc(b, "get_iq_tx") for b in boards]
        rebooted = {Antenna(b.host, a) for bi, b in enumerate(boards) for a in range(8) if before[bi]["sensors"][a]["boot_id"] != after[bi]["sensors"][a]["boot_id"]}
        # A reboot invalidates both RX and TX calibration, even if one is inactive.
        for g in calibration.groups:
            for c in g.corrections:
                bi = owner.hosts.index(c.state.antenna.host)
                if c.state.boot_id != after[bi]["sensors"][c.state.antenna.antid]["boot_id"]:
                    rebooted.add(c.state.antenna)
        calibration = calibration.without(rebooted)
        for bi, a in receivers:
            packet = packets[8 * bi + a][0][0]
            required = IQ_SYNC_INFO_GRID_SYNCED | IQ_SYNC_INFO_FIRE_TIME_VALID
            if packet.sync_info & required != required:
                raise RuntimeError("RX sample origin is not synchronized")
            states.append(CalibrationState.from_iq_packet(boards[bi].host, a, packet, after[bi]["sensors"][a]["boot_id"]))
        for bi, b in enumerate(boards):
            for sensor in after[bi]["sensors"]:
                if sensor["applied_mode"] == "iq_tx" and sensor["enabled"] and sensor.get("origin_ns"):
                    states.append(CalibrationState.from_tx_status(b.host, sensor))
        updated = calibration.retime(states)
        for bi, b in enumerate(boards):
            correction = {k: list(v) for k, v in after[bi]["calibration"].items()}
            touched = []
            for state in states:
                if state.direction != "tx" or state.antenna.host != b.host or not updated.covers([state]):
                    continue
                c = updated.correction(state)
                a = state.antenna.antid
                phase, delay = float(np.rad2deg(c.phase_rad)), c.delay_s * 80e6
                if abs(correction["phase_deg"][a] - phase) > 1e-5 or abs(correction["delay_samples"][a] - delay) > 1e-5:
                    correction["phase_deg"][a], correction["delay_samples"][a] = phase, delay
                    touched.append(a)
            if touched:
                _rpc(b, "set_iq_tx", {"calibration": correction})
                steering = after[bi]["steering"]
                capture.wait(
                    b,
                    lambda s: all(
                        abs(s["sensors"][a]["delay_samples"] - correction["delay_samples"][a] - steering["delay_samples"][a]) < 1e-3 and abs(s["sensors"][a]["phase_deg"] - correction["phase_deg"][a] - steering["phase_deg"][a]) < 0.02 for a in touched
                    ),
                    timeout,
                )
        # Check for a restart while the TX corrections were being applied.
        final = [_rpc(b, "get_iq_tx") for b in boards]
        if any((x["boot_id"], x["origin_ns"]) != (y["boot_id"], y["origin_ns"]) for old, new in zip(after, final) for x, y in zip(old["sensors"], new["sensors"])):
            raise RuntimeError("Sensor restarted while transferring calibration")
        owner.calibrations = updated
        receiver = owner._iq
        if receiver is not None and receiver._calibration is not None:
            epochs = np.zeros((len(boards), 2, 4), np.uint64)
            grid = np.zeros_like(epochs, dtype=np.int64)
            model = receiver._calibration
            for state in states:
                if state.direction != "rx" or not updated.covers([state]) or state.frequency_hz != receiver.calibration_center_frequency or state.sample_rate_hz != receiver.calibration_sample_rate:
                    continue
                bi = owner.hosts.index(state.antenna.host)
                pos = (bi, *boards[bi].revision.antenna_id_to_row_col(state.antenna.antid))
                c = updated.correction(state)
                model.timing_offsets[pos] = model.reference_path_delays[pos] - c.delay_s
                model.phase_offsets[pos] = -c.phase_rad - 2 * np.pi * state.frequency_hz * model.reference_path_delays[pos]
                epochs[pos], grid[pos] = state.epoch, state.sample_origin_ns
            receiver._calibration_epoch = epochs.tobytes()
            receiver._calibration_grid_origin = grid
        return updated
    except Exception:
        owner.calibrations = owner.calibrations.without(owner.antennas)
        for b in boards:
            try:
                _rpc(b, "set_iq_tx", {"muted": True})
            except Exception:
                pass
        raise
    finally:
        errors = []
        for bi in changed:
            try:
                boards[bi].iq.set_config({"trigger": configs[bi]["trigger"]})
            except Exception as error:
                errors.append(error)
        try:
            capture.close()
        except Exception as error:
            errors.append(error)
        if errors:
            raise ExceptionGroup("Could not restore capture triggers", errors)
