"""TX calibration using reciprocal links between sensors.

Configure IQ TX roles and source waveforms before calling ``IQPool.calibrate_tx``.
The boards must share their sample clock and REF source. Calibration temporarily
routes pilots through REF, with the antennas disconnected. Signals travel
between splitter outputs rather than backwards through the amplifier to the
controller. Their propagation response cancels in the two-way ratios.
"""

import copy
import json
import threading
import time
from collections import deque

from .iq_packet import IQChunkPacket, IQ_CHUNK_TYPE_HEADER, IQ_CHUNK_FLAG_REFERENCE, IQ_CHUNK_FLAG_REFERENCE_READY

import numpy as np


def fit_links(frequencies, response, valid):
    # response[f, receiver, transmitter] has the calibrated RX response removed.
    n = response.shape[1]
    if n == 1:
        return np.zeros(1), np.zeros(1), {"links": 0, "phase_residual_deg": 0.0, "delay_residual_samples": 0.0}
    h = np.zeros((n, n), complex)
    edges, slopes, weights = [], [], []
    for i in range(n):
        for j in range(i):
            good = valid[:, j, i] & valid[:, i, j]
            if good.sum() < 6 or np.ptp(frequencies[good]) < 10e6:
                continue
            z = response[good, j, i] / response[good, i, j]
            z /= abs(z)
            f = frequencies[good] / 80e6
            spacing = np.diff(f)
            nearest = np.isclose(spacing, spacing.min())
            coarse = np.angle(np.sum(z[1:][nearest] * z[:-1][nearest].conj())) / spacing.min()
            slope, phase = np.polyfit(f, np.unwrap(np.angle(z * np.exp(-1j * coarse * f))), 1)
            slope += coarse
            residual = np.angle(z * np.exp(-1j * (phase + slope * f)))
            rms = np.sqrt(np.mean(residual**2))
            if rms > 0.2:
                continue
            weight = 1 / max(0.02, rms) ** 2
            h[i, j] = weight * np.exp(1j * phase)
            h[j, i] = h[i, j].conjugate()
            edge = np.zeros(n - 1)
            if i:
                edge[i - 1] = 1
            if j:
                edge[j - 1] = -1
            edges.append(edge)
            slopes.append(slope / (2 * np.pi))
            weights.append(weight)
    if not edges or np.linalg.matrix_rank(edges) < n - 1:
        raise RuntimeError("Reciprocal links do not connect every selected transmitter")
    # Connection Laplacian avoids degree bias when some paths are weak.
    laplacian = np.diag(np.sum(abs(h), axis=1)) - h
    _, v = np.linalg.eigh(laplacian)
    phase = np.angle(v[:, 0] / v[0, 0])
    w = np.sqrt(weights)
    delay = np.r_[0, np.linalg.lstsq(np.asarray(edges) * w[:, None], np.asarray(slopes) * w, rcond=None)[0]]
    residual = [np.angle(h[i, j] * np.exp(-1j * (phase[i] - phase[j]))) for i in range(n) for j in range(i) if h[i, j]]
    return (
        -np.rad2deg(phase),
        delay,
        {
            "links": len(edges),
            "phase_residual_deg": float(np.rad2deg(np.sqrt(np.mean(np.square(residual))))),
            "delay_residual_samples": float(np.sqrt(np.mean((np.asarray(edges) @ delay[1:] - slopes) ** 2))),
        },
    )


def _rpc(board, path, payload=None):
    if payload is None:
        return board.control.get_json(path)
    result = board.control.post_json(path, payload)
    return None if result == "ok" else json.loads(result)


def _wait(board, predicate, timeout):
    deadline = time.monotonic() + timeout
    while True:
        state = _rpc(board, "get_iq_tx")
        if predicate(state):
            return state
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{board.host}: sensors did not acknowledge calibration settings")
        time.sleep(0.03)


def _waveform(board, recipe, timeout):
    generation = _rpc(board, "set_iq_tx_waveform", recipe)["generation"]
    state = _wait(board, lambda s: all(s["sensors"][a]["generation"] == generation for a in recipe["antennas"]), timeout)
    for a in recipe["antennas"]:
        if state["sensors"][a]["error"]:
            raise RuntimeError(f"{board.host}, sensor {a}: waveform error {state['sensors'][a]['error']}")
    return state


class _Captures:
    """Bounded collector for consecutive chunks, independent of complete RX pools."""

    def __init__(self, boards):
        self.changed = threading.Condition()
        self.ids = set()
        self.pending = {}
        self.frames = {}
        self.chunks = 1
        self.generations = {}
        self.not_before = {}
        self.subscriptions = [
            (
                b,
                b.subscribe_sensor_messages(IQ_CHUNK_TYPE_HEADER, lambda m, bi=bi: self.receive(8 * bi + m.antenna_id, m)),
            )
            for bi, b in enumerate(boards)
        ]

    def receive(self, sensor, message):
        try:
            packet = IQChunkPacket(message.payload)
        except ValueError:
            return
        if packet.flags & (IQ_CHUNK_FLAG_REFERENCE | IQ_CHUNK_FLAG_REFERENCE_READY):
            return
        with self.changed:
            if sensor not in self.ids or (sensor in self.generations and packet.config_generation != self.generations[sensor]):
                return
            # Queued UDP/SPI records may predate this pilot even when their
            # configuration generation is unchanged on another board.
            captured_ns = packet.fire_time_ns + round(packet.chunk_counter * 256e9 / packet.sample_rate_hz)
            if sensor in self.not_before:
                # The hardware chunk counter wraps after 2**24 chunks.
                span_ns = round((1 << 24) * 256e9 / packet.sample_rate_hz)
                age = (captured_ns - self.not_before[sensor] + span_ns // 2) % span_ns - span_ns // 2
                if age < 0:
                    return
            index = packet.source_chunk_index & 0xFFFFFF
            key = (packet.fire_time_ns, packet.config_generation, index // self.chunks)
            pending = self.pending.setdefault(sensor, {})
            pending.setdefault(key, {})[index % self.chunks] = packet
            if len(pending[key]) == self.chunks:
                self.frames[sensor].append([pending[key][i] for i in range(self.chunks)])
                del pending[key]
                self.changed.notify_all()
            while len(pending) > 4:
                del pending[next(iter(pending))]

    def collect(self, ids, samples, count, timeout, generations=None, not_before=None):
        if not ids:
            return {}
        deadline = time.monotonic() + timeout
        with self.changed:
            self.ids = set(ids)
            self.generations = generations or {}
            self.not_before = not_before or {}
            self.chunks = samples // 256
            self.pending = {}
            self.frames = {i: deque(maxlen=count) for i in ids}
            try:
                while any(len(v) < count for v in self.frames.values()):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        missing = [i for i, v in self.frames.items() if len(v) < count]
                        raise RuntimeError(f"Missing calibration captures from sensors {missing}")
                    self.changed.wait(left)
                return {i: list(v) for i, v in self.frames.items()}
            finally:
                self.ids = set()

    def close(self):
        for board, subscription in self.subscriptions:
            board.unsubscribe_sensor_messages(subscription)


def _grid_origin(packet):
    base = (packet.source_chunk_index - packet.chunk_counter) & 0xFFFFFF
    return packet.fire_time_ns - round(base * 256e9 / packet.sample_rate_hz)


def _transmitter_settings(state):
    keys = ("antid", "frequency_hz", "rf_gain", "bandwidth_mhz", "output", "amplitude")
    return [{key: sensor[key] for key in keys} for sensor in state["sensors"]]


def calibrate(pool, *, rx_gain=None, cable_lengths=None, cable_velocity_factors=None, timeout=10.0, pilot_output="reference"):
    """Calibrate the currently selected transmitters and restore RX/TX operation.

    Selected transmitters need a waveform with a common replay length and
    center frequency. Active receivers must use that frequency too. This
    preserves uploaded/synthesized waveforms, gain, steering and mute settings.
    On failure transmitters become silent receivers, and calibration is invalid.

    Returns per-board calibration dictionaries and link-fit residuals. The
    corrections are already applied. RX calibration remains available on the
    pool for sensors restored to IQ RX mode.

    Pilots use REF by default. Set ``pilot_output="antenna_l"`` explicitly
    for over-the-air calibration when REF outputs cannot communicate.
    """
    if pilot_output not in ("reference", "antenna_l"):
        raise ValueError("pilot_output must be 'reference' or 'antenna_l'")
    if pool._calibrating:
        raise RuntimeError("Calibration is already running")
    boards = pool.boards
    saved_rx = [b.iq.get_config() for b in boards]
    saved_tx = [_rpc(b, "get_iq_tx") for b in boards]
    targets = [(bi, a) for bi, c in enumerate(saved_rx) for a, mode in enumerate(c["modes"]) if mode == "iq_tx"]
    if not targets:
        raise ValueError("Select IQ TX for at least one sensor before calibrating")
    source = [saved_tx[bi]["sensors"][a] for bi, a in targets]
    length = source[0]["samples"]
    center = source[0]["frequency_hz"]
    if not length or any(s["samples"] != length for s in source):
        raise ValueError("Prepare TX waveforms with one common sample count before calibrating")
    if any(s["frequency_hz"] != center for s in source):
        raise ValueError("Selected transmitters must use one common frequency")
    receivers = [(bi, a) for bi, c in enumerate(saved_rx) for a, mode in enumerate(c["modes"]) if mode == "iq_rx"]
    if any(saved_rx[bi]["receivers"][a]["rf_freq_hz"] != center for bi, a in receivers):
        raise ValueError("Active receivers and transmitters must use one common frequency")
    if len({saved_rx[bi]["adc_decimation"] for bi, _ in receivers}) > 1:
        raise ValueError("Active receivers must use one common sample rate")
    period = min(length, 1024)
    bins = np.array([k for k in range(-period * 3 // 16, period * 3 // 16 + 1) if k and (k % 8 == 0 or abs(k) == period // 16 + 1)])
    frequencies = bins * 80e6 / period
    target_ids = [8 * bi + a for bi, a in targets]
    positions = [(bi, *boards[bi].revision.antenna_id_to_row_col(a)) for bi, a in targets]
    zero = {"phase_deg": [0] * 8, "delay_samples": [0] * 8}
    pilots = set()
    monitors = [_rpc(b, "get_iq_monitor") for b in boards]
    capture = _Captures(boards)
    success = False
    try:
        for b in boards:
            _rpc(b, "set_iq_monitor", {"enabled": False})
            _rpc(b, "set_iq_tx", {"muted": True, "modes": ["iq_rx"] * 8, "calibration": zero, "steering": zero})
        if rx_gain is None:
            rx_gain = 42 if pool.master_board.wifi_rx.get_config()["calib-source"] else 30
        pool.start()
        pool.apply_config(
            {
                "mode": "iq",
                "rf_freq_hz": center,
                "adc_decimation": 1,
                "gain_mode": "manual",
                "rx_gain": rx_gain,
                "rf_switch": 1 if pilot_output == "reference" else 3,
                "trigger": {"mode": "interval", "interval_chunks": 8192, "burst_chunks": 4},
            }
        )
        rx_cal = pool.sync(
            center_hz=center,
            rx_gain=rx_gain,
            cable_lengths=cable_lengths,
            cable_velocity_factors=cable_velocity_factors,
        )
        pool._calibrating = True
        rx_origins = pool._calibration_grid_origin.copy()
        pool._calibration = None
        reference = np.array([rx_origins[p] for p in positions], dtype=np.int64)
        phases = np.array([rx_cal.phase_offsets[p] for p in positions])
        timing = np.array([rx_cal.timing_offsets[p] for p in positions])
        paths = np.array([rx_cal.reference_path_delays[p] for p in positions])
        paths -= paths[0]
        boot_ids = [[s["boot_id"] for s in _rpc(b, "get_iq_tx")["sensors"]] for b in boards]
        count = len(targets)
        response = np.zeros((len(bins), count, count), complex)
        valid = np.zeros(response.shape, bool)
        for tx, (bi, ant) in enumerate(targets):
            board = boards[bi]
            pool._logger.info("TX calibration: sensor %s/%d (%d/%d)", board.host, ant, tx + 1, count)
            _wait(board, lambda s: all(x["applied_mode"] == "iq_rx" for x in s["sensors"]), timeout)
            _waveform(
                board,
                {"type": "calibration", "samples": length, "amplitude": 0.1, "variant": 0, "antennas": [ant]},
                timeout,
            )
            pilots.add((bi, ant))
            _rpc(
                board,
                "set_iq_tx",
                {
                    "muted": False,
                    "transmitters": [{"antid": ant, "output": pilot_output, "amplitude": 1}],
                    "modes": ["iq_tx" if a == ant else "iq_rx" for a in range(8)],
                },
            )
            state = _wait(board, lambda s: s["sensors"][ant]["enabled"] and s["sensors"][ant]["applied_mode"] == "iq_tx", timeout)
            origin = state["sensors"][ant].get("origin_ns", 0)
            if not origin:
                raise RuntimeError("Firmware does not report a synchronized TX replay origin")
            # Translate the TX start to each receiver's synchronized local clock.
            # Require a complete pilot period before accepting samples.
            earliest = {sensor: int(reference[rx]) + origin - int(reference[tx]) + period * 12.5 for rx, sensor in enumerate(target_ids) if rx != tx}
            frames = capture.collect(list(earliest), period, 20, timeout, not_before=earliest)
            latency = ((origin - int(reference[tx]) + period * 6.25) % (period * 12.5) - period * 6.25) * 1e-9
            for rx, sensor in enumerate(target_ids):
                if rx == tx:
                    continue
                spectra = []
                for frame in frames[sensor]:
                    z = np.concatenate([p.decode_iq() for p in frame])
                    if max(abs(z.real).max(), abs(z.imag).max()) >= 500:
                        continue
                    p = frame[0]
                    if any(q.fire_time_ns != p.fire_time_ns for q in frame):
                        continue
                    delta_ns = p.fire_time_ns - int(reference[rx]) + (p.chunk_counter % (period // 256)) * 3200
                    delta = ((delta_ns + period * 6.25) % (period * 12.5) - period * 6.25) * 1e-9
                    rx_phase = phases[rx] + 2 * np.pi * frequencies * (-timing[rx] + delta) + 2 * np.pi * (center + frequencies) * paths[rx]
                    spectra.append(np.fft.fft(z)[bins % period] / period * np.exp(-1j * rx_phase + 2j * np.pi * frequencies * latency))
                if len(spectra) < 8:
                    continue
                spectra = np.array(spectra)
                mean = spectra.mean(axis=0)
                power = np.mean(abs(spectra) ** 2, axis=0)
                response[:, rx, tx] = mean
                valid[:, rx, tx] = abs(mean) ** 2 > 0.9 * power
            check = _rpc(board, "get_iq_tx")
            if [s["boot_id"] for s in check["sensors"]] != boot_ids[bi] or check["sensors"][ant]["origin_ns"] != origin:
                raise RuntimeError("A transmitter restarted during calibration")
            _rpc(board, "set_iq_tx", {"muted": True, "modes": ["iq_rx"] * 8})
        phase, delay, quality = fit_links(frequencies, response, valid)
        if quality["phase_residual_deg"] > 5 or quality["delay_residual_samples"] > 0.3:
            raise RuntimeError(f"Reciprocal calibration did not validate: {quality}")
        for bi, b in enumerate(boards):
            _wait(b, lambda s: all(x["applied_mode"] == "iq_rx" for x in s["sensors"]), timeout)
            ants = [a for j, a in pilots if j == bi]
            if ants:
                _waveform(b, {"type": "restore", "samples": length, "antennas": ants}, timeout)
            _rpc(
                b,
                "set_iq_tx",
                {
                    "muted": True,
                    "transmitters": _transmitter_settings(saved_tx[bi]),
                    "steering": saved_tx[bi]["steering"],
                },
            )
            s = saved_rx[bi]
            # Restore receiver preferences and roles atomically. Retuning a
            # still-receiving TX sensor to its saved RX frequency would lose
            # the PLL phase measured above.
            b.iq.set_config(
                {
                    "modes": s["modes"],
                    "receivers": s["receivers"],
                    "rf_switch": s["rf_switch"],
                    "adc_decimation": s["adc_decimation"],
                    "trigger": {"mode": "interval", "interval_chunks": 8192, "burst_chunks": 1},
                }
            )
        final = [
            _wait(
                b,
                lambda s, bi=bi: all(x["applied_mode"] == saved_rx[bi]["modes"][a] and (x["applied_mode"] != "iq_tx" or x["enabled"] and x.get("origin_ns")) for a, x in enumerate(s["sensors"])),
                timeout,
            )
            for bi, b in enumerate(boards)
        ]
        latency = np.array([((final[bi]["sensors"][a]["origin_ns"] - int(reference[i]) + length * 6.25) % (length * 12.5) - length * 6.25) / 12.5 for i, (bi, a) in enumerate(targets)])
        delay -= latency - latency[0]
        corrections = [copy.deepcopy(s["calibration"]) for s in saved_tx]
        for i, (bi, a) in enumerate(targets):
            corrections[bi]["phase_deg"][a] = float(phase[i])
            corrections[bi]["delay_samples"][a] = float(delay[i])
        for bi, b in enumerate(boards):
            _rpc(b, "set_iq_tx", {"calibration": corrections[bi]})
            ants = [a for j, a in targets if j == bi]
            state = _wait(
                b,
                lambda s: all(
                    abs(s["sensors"][a]["phase_deg"] - corrections[bi]["phase_deg"][a] - saved_tx[bi]["steering"]["phase_deg"][a]) < 0.02
                    and abs(s["sensors"][a]["delay_samples"] - corrections[bi]["delay_samples"][a] - saved_tx[bi]["steering"]["delay_samples"][a]) < 1e-4
                    for a in ants
                ),
                timeout,
            )
            if [s["boot_id"] for s in state["sensors"]] != boot_ids[bi] or any(state["sensors"][a]["origin_ns"] != final[bi]["sensors"][a]["origin_ns"] for a in ants):
                raise RuntimeError("A transmitter restarted while applying calibration")
        # Retain valid RX calibration when restoring a lower receive sample rate.
        generations = [b.iq.get_config()["config_generation"] for b in boards]
        packets = capture.collect([8 * bi + a for bi, a in receivers], 256, 1, timeout, {8 * bi + a: generations[bi] for bi, a in receivers})
        epoch = np.zeros(rx_cal.sensor_shape, np.uint64)
        grid = np.zeros(rx_cal.sensor_shape, np.int64)
        changes = []
        for bi, a in receivers:
            p = packets[8 * bi + a][0][0]
            pos = (bi, *boards[bi].revision.antenna_id_to_row_col(a))
            epoch[pos] = p.fire_time_ns
            grid[pos] = _grid_origin(p)
            changes.append((pos, ((int(grid[pos]) - int(rx_origins[pos]) + 26843545600) % 53687091200 - 26843545600) * 1e-9))
        reference_change = changes[0][1] if changes else 0
        for pos, change in changes:
            rx_cal.timing_offsets[pos] -= change - reference_change
        for bi, b in enumerate(boards):
            b.iq.set_config({"trigger": saved_rx[bi]["trigger"]})
            _rpc(b, "set_iq_tx", {"muted": saved_tx[bi]["muted"]})
        pool._calibration = rx_cal if receivers else None
        pool._calibration_epoch = epoch.tobytes()
        pool._calibration_grid_origin = grid
        if receivers:
            from .iq_packet import DECIM_TO_FS

            pool._calibration_sample_rate = DECIM_TO_FS[saved_rx[receivers[0][0]]["adc_decimation"]]
        success = True
        return {"calibration": corrections, **quality}
    finally:
        errors = []
        if not success:
            pool._calibration = None
            for bi, b in enumerate(boards):
                try:
                    _rpc(
                        b,
                        "set_iq_tx",
                        {"muted": True, "modes": ["iq_rx" if m == "iq_tx" else m for m in saved_rx[bi]["modes"]]},
                    )
                    ants = [a for j, a in pilots if j == bi]
                    if ants:
                        _waveform(b, {"type": "restore", "samples": length, "antennas": ants}, timeout)
                    _rpc(
                        b,
                        "set_iq_tx",
                        {
                            "muted": True,
                            "transmitters": _transmitter_settings(saved_tx[bi]),
                            "steering": saved_tx[bi]["steering"],
                        },
                    )
                    b.iq.set_config({k: saved_rx[bi][k] for k in ("receivers", "rf_switch", "adc_decimation", "trigger")})
                except Exception as error:
                    errors.append(f"{b.host}: {error}")
        try:
            pool.master_board.iq.set_reference()
        except Exception as error:
            errors.append(str(error))
        for b, monitor in zip(boards, monitors):
            try:
                _rpc(
                    b,
                    "set_iq_monitor",
                    {
                        k: monitor[k]
                        for k in (
                            "enabled",
                            "gain_mode",
                            "frequency_hz",
                            "sample_rate_hz",
                            "gain",
                            "bandwidth_mhz",
                            "samples",
                            "interval_us",
                            "offset_us",
                        )
                    },
                )
            except Exception as error:
                errors.append(f"{b.host}: {error}")
        capture.close()
        pool._calibrating = False
        if errors:
            pool._calibration = None
            for b in boards:
                try:
                    _rpc(b, "set_iq_tx", {"muted": True})
                except Exception as error:
                    errors.append(f"{b.host}: could not mute TX ({error})")
            raise RuntimeError("Calibration settings could not be restored: " + "; ".join(errors))
