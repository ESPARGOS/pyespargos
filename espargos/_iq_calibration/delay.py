"""Measure TX–RX delay using a known reference path."""

import logging
import math
import random
import numpy as np

from ..calibration import Antenna
from .capture import _Captures, _grid_origin
from ..iq_tx_rx_delay import TxRxDelayCalibration
from ..iq_tx_control import _rpc, _waveform
from .pilots import pilots
from ..sensor_calibration import compute_reference_path_delays

_GROUP_TOLERANCE_S = 0.25 / 80e6


def _delay(response, sample_rate):
    """Estimate probe delay from the peak of its impulse response."""
    samples = response.shape[-1]
    padding = 15 * samples
    spectrum = np.pad(np.fft.fftshift(response, axes=-1), [(0, 0)] * (response.ndim - 1) + [(padding // 2, padding - padding // 2)])
    impulse = np.fft.ifft(np.fft.ifftshift(spectrum, axes=-1), axis=-1)
    power = np.mean(abs(impulse.reshape(-1, 16 * samples)) ** 2, axis=0)
    index = int(np.argmax(power))
    if not np.isfinite(power).all() or power[index] <= 0 or power[index] < 8 * np.median(power):
        raise ValueError("No clear reference path for TX–RX timing")
    left, middle, right = power[(index - 1) % len(power)], power[index], power[(index + 1) % len(power)]
    curvature = left - 2 * middle + right
    fraction = 0.5 * (left - right) / curvature if curvature < 0 else 0
    return float((index + fraction) / (16 * sample_rate)) % (samples / sample_rate)


def _delay_cluster(values, period):
    """Largest set of delays spanning at most a quarter of an ADC sample."""
    if not len(values):
        return np.empty(0)
    values = np.sort(np.asarray(values) % period)
    wrapped = np.concatenate((values, values + period))
    # Search on the circle: two estimates on either side of sample zero agree.
    ends = np.searchsorted(wrapped, values + _GROUP_TOLERANCE_S, side="right")
    counts = ends - np.arange(len(values))
    start = int(np.argmax(counts))
    return wrapped[start : ends[start]]


def _group_probe_delay(responses, capture_period, ambiguity):
    """Estimate each link independently, checking it against held-out frames."""
    links = []
    for h in responses:
        first, second = h.reshape(2, 8, capture_period).mean(axis=1)
        try:
            a, b = _delay(first, 80e6), _delay(second, 80e6)
        except ValueError:
            continue  # No clear impulse peak in one of the batches.
        delta = (b - a + ambiguity / 2) % ambiguity - ambiguity / 2
        if abs(delta) <= _GROUP_TOLERANCE_S:
            links.append((a + delta / 2) % ambiguity)
    cluster = _delay_cluster(links, ambiguity)
    if len(cluster) > len(links) // 2:
        return float(np.median(cluster) % ambiguity)
    return None


def measure(owner, antennas, path, reference_delays_s, timeout, *, method="per_tx"):
    """Measure with calibration probes, then restore the application waveforms."""
    if method not in ("group", "per_tx"):
        raise ValueError("Choose group or per_tx timing calibration")
    group = method == "group"
    routes = {"reference": 1, "antenna_r": 2, "antenna_l": 3}
    if path not in routes:
        raise ValueError("Choose reference, antenna_r or antenna_l")
    if path != "reference" and reference_delays_s is None:
        raise ValueError("Antenna-path timing requires reference_delays_s for each TX/RX pair")
    boards = owner.boards
    configs = [b.iq.get_config() for b in boards]
    tx_states = [_rpc(b, "get_iq_tx") for b in boards]
    tx = [Antenna(b.host, a) for b, c in zip(boards, configs) for a, mode in enumerate(c["modes"]) if mode == "iq_tx" and Antenna(b.host, a) in antennas]
    rx = [Antenna(b.host, a) for b, c in zip(boards, configs) for a, mode in enumerate(c["modes"]) if mode == "iq_rx" and Antenna(b.host, a) in antennas]
    if not rx or not tx:
        raise ValueError("Timing calibration needs running IQ receivers and transmitters")
    board_index = {b.host: i for i, b in enumerate(boards)}
    for bi, b in enumerate(boards):
        if any(a.host == b.host for a in rx) and configs[bi]["adc_decimation"] != 1 and any(mode == "iq_rx" and Antenna(b.host, a) not in rx for a, mode in enumerate(configs[bi]["modes"])):
            raise ValueError("Full-rate timing probes require all active receivers on each affected board")
    ids = [8 * board_index[a.host] + a.antid for a in rx]
    paths = compute_reference_path_delays(boards, owner._geometry.cables, owner._geometry.velocity_factors) if reference_delays_s is None else None

    def propagation(transmitter, receiver):
        if reference_delays_s is not None:
            return float(reference_delays_s[transmitter, receiver])
        total = 0
        for a in (transmitter, receiver):
            bi = board_index[a.host]
            total += paths[(bi, *boards[bi].revision.antenna_id_to_row_col(a.antid))]
        return total

    delays = {(transmitter, receiver): propagation(transmitter, receiver) for transmitter in tx for receiver in rx if not group or reference_delays_s is None or (transmitter, receiver) in reference_delays_s}
    if any(not np.isfinite(v) or v < 0 for v in delays.values()):
        raise ValueError("Reference propagation delays must be finite and nonnegative")
    probe_tx = [a for a in tx if any((a, r) in delays for r in rx)]
    minimum_tx = min(3, len(tx))
    if group:
        if len(probe_tx) < minimum_tx:
            raise ValueError(f"Group timing needs known paths from at least {minimum_tx} transmitters")
        random.shuffle(probe_tx)
    calibration = owner.refresh_calibration(timeout)
    scoped = calibration.select(antennas=rx, direction="rx").merge(calibration.select(antennas=tx, direction="tx"))
    corrections = {(c.state.antenna, c.state.direction): c for g in scoped.groups for c in g.corrections}
    if any((a, role) not in corrections for ants, role in [(rx, "rx"), (tx, "tx")] for a in ants):
        raise ValueError("Calibrate all selected IQ receivers and transmitters first")
    TxRxDelayCalibration(scoped, {a: 0.0 for a in tx})  # Validate coherent group scope before transmitting.
    if any(corrections[a, "rx"].state.sample_origin_ns is None for a in rx):
        raise ValueError("Timing probes require calibrated RX sample origins")
    # A 32 MSa/s pilot can occupy a fractional number of 256-sample RX
    # chunks. Collect whole pilot periods and whole transport chunks.
    periods = {}
    period_limit = 1024
    if group:
        shortest = min(tx_states[board_index[a.host]]["sensors"][a.antid]["samples"] for a in probe_tx)
        period_limit = min(shortest, period_limit)
    for transmitter in probe_tx:
        state = tx_states[board_index[transmitter.host]]["sensors"][transmitter.antid]
        period = min(state["samples"], period_limit)
        rate = state.get("sample_rate_hz", 80000000)
        # Longer bursts exceed the receiver's reliable eight-chunk copy window.
        while period * 80000000 // rate > 2048:
            period //= 2
        rx_period = period * 80000000 // rate
        periods[transmitter] = (period, rx_period, math.lcm(rx_period, 256))
    # Non-power-of-two windows need not align with the interval trigger.
    burst_chunks = max(v[2] for v in periods.values()) // 256
    capture = _Captures(boards)
    prepared = set()
    measured = {}
    success = False
    try:
        for bi, b in enumerate(boards):
            _rpc(b, "set_iq_tx", {"muted": True, "steering": {"phase_deg": [0] * 8, "delay_samples": [0] * 8}})
            decimation = 1 if any(a.host == b.host for a in rx) else configs[bi]["adc_decimation"]
            b.iq.set_config(
                {
                    "adc_decimation": decimation,
                    "rf_switch": routes[path],
                    "receivers": [{"antid": a.antid, "rx_gain": 24, "gain_mode": "manual"} for a in rx if a.host == b.host],
                    "trigger": {"mode": "interval", "interval_chunks": 8192, "burst_chunks": burst_chunks},
                }
            )
        for transmitter in probe_tx:
            bi = board_index[transmitter.host]
            b = boards[bi]
            length = tx_states[bi]["sensors"][transmitter.antid]["samples"]
            prepared.add(transmitter)
            _waveform(b, {"type": "calibration", "samples": length, "amplitude": 0.25, "pilot_mask": 255, "pilot_samples": periods[transmitter][0], "antennas": [transmitter.antid]}, timeout, wait=capture.wait)
            _rpc(b, "set_iq_tx", {"muted": False, "transmitters": [{"antid": a, "amplitude": 1 if a == transmitter.antid else 0, "output": path, "rf_gain": {"a": 0, "b": 0}} for a in range(8)]})
            capture.wait(b, lambda s: abs(s["sensors"][transmitter.antid]["applied_amplitude"] - 1) < 1e-6, timeout)
            board_generations = [b.iq.get_config()["config_generation"] for b in boards]
            generations = {i: board_generations[i // 8] for i in ids}
            # Discard buffered captures from before the pilot started.
            # The short pilot repeats without changing the replay buffer length.
            period, rx_period, capture_period = periods[transmitter]
            active_ids = [i for r, i in zip(rx, ids) if (transmitter, r) in delays]
            settled = capture.collect(active_ids, capture_period, 2, timeout, generations, allow_missing=group)
            frames = capture.collect(list(settled), capture_period, 16, timeout, generations, allow_missing=group)
            bins, spectrum, _ = pilots(period, peak=128, mask=255)[0]
            capture_bins = bins * (capture_period // rx_period)
            frequencies = np.fft.fftfreq(capture_period, 1 / 80e6)
            responses = []
            for receiver, i in zip(rx, ids):
                if i not in frames:
                    continue
                correction = corrections[receiver, "rx"]
                chunks = frames[i]
                origins = [_grid_origin(frame[0]) for frame in chunks]
                wrap_ns = 53687091200
                if any((origin - origins[0]) % wrap_ns for origin in origins):
                    raise RuntimeError("RX origin changed during timing calibration")
                # Refer full-rate probes back to the calibrated receive grid.
                delta = (origins[0] - correction.state.sample_origin_ns + wrap_ns // 2) % wrap_ns - wrap_ns // 2
                shift_s = ((2 * int(delta) + 12) // 25) / 80e6
                z = np.asarray([np.concatenate([p.decode_iq() for p in frame]) for frame in chunks])
                if max(abs(z.real).max(), abs(z.imag).max()) >= 500:
                    if group:
                        continue
                    raise ValueError("Timing calibration clips the receiver")
                h = np.zeros(z.shape, complex)
                h[:, capture_bins % capture_period] = np.fft.fft(z, axis=-1)[:, capture_bins % capture_period] / spectrum[bins % period]
                h *= correction.response(correction.state.frequency_hz + frequencies) * np.exp(-2j * np.pi * frequencies * shift_s)
                h *= np.exp(2j * np.pi * frequencies * delays[transmitter, receiver])
                responses.append(h)
            _rpc(b, "set_iq_tx", {"muted": True})
            ambiguity = rx_period / 80e6
            if group:
                delay = _group_probe_delay(responses, capture_period, ambiguity)
                if delay is not None:
                    measured[transmitter] = delay
                cluster = _delay_cluster(list(measured.values()), ambiguity)
                support = len(cluster)
                dissent = len(measured) - support
                if support >= minimum_tx and support > dissent:
                    logging.getLogger(__name__).info("Group range zero: %d TX agree after probing %d of %d", support, len(prepared), len(tx))
                    measured = dict.fromkeys(tx, float(np.median(cluster) % ambiguity))
                    break
                # Even unanimous remaining probes cannot rescue this measurement.
                remaining = len(probe_tx) - len(prepared)
                if support + remaining < minimum_tx or support + remaining <= dissent:
                    raise ValueError(f"No common TX–RX delay: {support} agreeing TXs, {len(measured)} usable, {len(prepared)} probed")
            else:
                first, second = np.asarray(responses).reshape(len(rx), 2, 8, capture_period).mean(axis=2).transpose(1, 0, 2)
                delay, checked = _delay(first, 80e6) % ambiguity, _delay(second, 80e6) % ambiguity
                if abs((checked - delay + ambiguity / 2) % ambiguity - ambiguity / 2) > 1 / 80e6:
                    raise ValueError("TX–RX timing changed between reference captures")
                measured[transmitter] = delay
        final = [_rpc(b, "get_iq_tx") for b in boards]
        if any((s["boot_id"], s["origin_ns"]) != (v["boot_id"], v["origin_ns"]) for old, new in zip(tx_states, final) for s, v in zip(old["sensors"], new["sensors"])):
            raise RuntimeError("Sensor restarted during timing calibration")
        result = TxRxDelayCalibration(scoped, measured)
        success = True
        return result
    finally:
        errors = []

        def attempt(action, *args, **kwargs):
            try:
                action(*args, **kwargs)
            except Exception as error:
                errors.append(error)

        # Try every board even if one fails. Keep TX muted if restoration fails.
        for bi, b in enumerate(boards):
            attempt(_rpc, b, "set_iq_tx", {"muted": True})
            for transmitter in prepared:
                if transmitter.host == b.host:
                    attempt(_waveform, b, {"type": "restore", "samples": tx_states[bi]["sensors"][transmitter.antid]["samples"], "antennas": [transmitter.antid]}, timeout, wait=capture.wait)
            keys = ("antid", "output", "amplitude", "rf_gain")
            attempt(_rpc, b, "set_iq_tx", {"steering": tx_states[bi]["steering"], "transmitters": [{k: s[k] for k in keys} for s in tx_states[bi]["sensors"]]})
            attempt(b.iq.set_config, {k: configs[bi][k] for k in ("rf_switch", "receivers", "trigger", "adc_decimation")})
        attempt(capture.close)
        if success and not errors:
            for b, state in zip(boards, tx_states):
                attempt(_rpc, b, "set_iq_tx", {"muted": state["muted"]})
                if errors:
                    break
        if errors:
            for b in boards:
                attempt(_rpc, b, "set_iq_tx", {"muted": True})
            raise ExceptionGroup("Could not restore settings after TX–RX delay calibration", errors)
