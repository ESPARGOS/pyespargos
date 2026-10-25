"""Measure TX–RX delay using a known reference path."""

import numpy as np

from ..calibration import Antenna
from .capture import _Captures, _grid_origin
from ..iq_tx_rx_delay import TxRxDelayCalibration
from ..iq_tx_control import _rpc, _waveform
from .pilots import pilots
from ..sensor_calibration import compute_reference_path_delays


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


def measure(owner, antennas, path, reference_delays_s, timeout):
    """Measure with calibration probes, then restore the application waveforms."""
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

    delays = {(transmitter, receiver): propagation(transmitter, receiver) for transmitter in tx for receiver in rx}
    if any(not np.isfinite(v) or v < 0 for v in delays.values()):
        raise ValueError("Reference propagation delays must be finite and nonnegative")
    calibration = owner.refresh_calibration(timeout)
    scoped = calibration.select(antennas=rx, direction="rx").merge(calibration.select(antennas=tx, direction="tx"))
    corrections = {(c.state.antenna, c.state.direction): c for g in scoped.groups for c in g.corrections}
    if any((a, role) not in corrections for ants, role in [(rx, "rx"), (tx, "tx")] for a in ants):
        raise ValueError("Calibrate all selected IQ receivers and transmitters first")
    TxRxDelayCalibration(scoped, {a: 0.0 for a in tx})  # Validate coherent group scope before transmitting.
    if any(corrections[a, "rx"].state.sample_origin_ns is None for a in rx):
        raise ValueError("Timing probes require calibrated RX sample origins")
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
                    "trigger": {"mode": "interval", "interval_chunks": 8192, "burst_chunks": 4},
                }
            )
        for transmitter in tx:
            bi = board_index[transmitter.host]
            b = boards[bi]
            length = tx_states[bi]["sensors"][transmitter.antid]["samples"]
            prepared.add(transmitter)
            _waveform(b, {"type": "calibration", "samples": length, "amplitude": 0.25, "pilot_mask": 255, "antennas": [transmitter.antid]}, timeout, wait=capture.wait)
            _rpc(b, "set_iq_tx", {"muted": False, "transmitters": [{"antid": a, "amplitude": 1 if a == transmitter.antid else 0, "output": path, "rf_gain": {"a": 0, "b": 0}} for a in range(8)]})
            capture.wait(b, lambda s: abs(s["sensors"][transmitter.antid]["applied_amplitude"] - 1) < 1e-6, timeout)
            board_generations = [b.iq.get_config()["config_generation"] for b in boards]
            generations = {i: board_generations[i // 8] for i in ids}
            # Discard buffered captures from before the pilot started.
            # Firmware repeats a 1024-sample pilot in longer replay buffers.
            period = min(length, 1024)
            capture.collect(ids, period, 2, timeout, generations)
            frames = capture.collect(ids, period, 16, timeout, generations)
            bins, spectrum, _ = pilots(period, peak=128, mask=255)[0]
            frequencies = np.fft.fftfreq(period, 1 / 80e6)
            responses = []
            for receiver, i in zip(rx, ids):
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
                    raise ValueError("Timing calibration clips the receiver")
                h = np.zeros(z.shape, complex)
                h[:, bins % period] = np.fft.fft(z, axis=-1)[:, bins % period] / spectrum[bins % period]
                h *= correction.response(correction.state.frequency_hz + frequencies) * np.exp(-2j * np.pi * frequencies * shift_s)
                h *= np.exp(2j * np.pi * frequencies * delays[transmitter, receiver])
                responses.append(h)
            first, second = np.asarray(responses).reshape(len(rx), 2, 8, period).mean(axis=2).transpose(1, 0, 2)
            delay, checked = _delay(first, 80e6), _delay(second, 80e6)
            ambiguity = period / 80e6
            if abs((checked - delay + ambiguity / 2) % ambiguity - ambiguity / 2) > 1 / 80e6:
                raise ValueError("TX–RX timing changed between reference captures")
            measured[transmitter] = delay
            _rpc(b, "set_iq_tx", {"muted": True})
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
