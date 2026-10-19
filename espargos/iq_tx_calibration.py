"""TX calibration using reciprocal links between sensors.

Configure IQ TX roles and source waveforms before calling ``IQPool.calibrate_tx``.
The boards must share their sample clock and REF source. Calibration temporarily
routes pilots through REF, with the antennas disconnected. Signals travel
between splitter outputs rather than backwards through the amplifier to the
controller. Their propagation response cancels in the two-way ratios.
"""

import copy
import json
import struct
import zlib
import threading
import time
from collections import deque
from .iq_reference import _map
from .iq_tx_calibration_fit import fit_links

from .iq_packet import IQChunkPacket, IQ_CHUNK_TYPE_HEADER, IQ_CHUNK_FLAG_REFERENCE, IQ_CHUNK_FLAG_REFERENCE_READY

import numpy as np


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


def _waveform(board, recipe, timeout, wait=_wait, retry=False):
    started = time.monotonic()
    antennas = recipe["beams"][0]["waveform"]["antennas"] if "beams" in recipe else recipe["antennas"]
    generation = _rpc(board, "set_iq_tx_waveform", recipe)["generation"]

    def applied(state):
        for a in antennas:
            sensor = state["sensors"][a]
            if sensor["generation"] == generation and sensor["error"]:
                raise RuntimeError(f"{board.host}, sensor {a}: waveform error {sensor['error']}")
        return all(state["sensors"][a]["generation"] == generation for a in antennas)

    try:
        state = wait(board, applied, min(timeout, 0.25) if retry else timeout)
    except TimeoutError:
        if not retry:
            raise
        # RX can lose a UART command while servicing a capture. These immediate
        # recipes are safe to repeat, including on sensors that applied them.
        # Sensor-reported errors are never retried.
        generation = _rpc(board, "set_iq_tx_waveform", recipe)["generation"]
        state = wait(board, applied, max(0, timeout - (time.monotonic() - started)))
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
        self.status = {b: [None] * 8 for b in boards}
        self.subscriptions = []
        try:
            for bi, b in enumerate(boards):
                self.subscriptions.append((b, b.subscribe_sensor_messages(IQ_CHUNK_TYPE_HEADER, lambda m, bi=bi: self.receive(8 * bi + m.antenna_id, m))))
                self.subscriptions.append((b, b.subscribe_sensor_messages(0xA3F1720B, lambda m, b=b: self.receive_status(b, m))))
        except Exception:
            self.close()
            raise

    def receive_status(self, board, message):
        data = message.payload
        offset, state = 4, None
        while offset + 3 <= len(data):
            tag, size = struct.unpack_from("<BH", data, offset)
            start = offset
            offset += 3
            if offset + size > len(data):
                return
            if tag == 6 and size == 108:
                v = struct.unpack_from("<9I3f15I", data, offset)
                if v[13] > 2:
                    return
                state = dict(
                    zip(
                        (
                            "generation",
                            "samples",
                            "crc32",
                            "error",
                            "enabled",
                            "clipped_samples",
                            "update_us",
                            "boot_id",
                            "sequence",
                            "phase_deg",
                            "delay_samples",
                            "applied_amplitude",
                        ),
                        v[:12],
                    )
                )
                state.update(applied_mode=("wifi", "iq_rx", "iq_tx")[v[13]], origin_ns=v[25] | v[26] << 32)
            if tag == 255:
                if size != 4 or offset + size != len(data) or zlib.crc32(data[:start]) != struct.unpack_from("<I", data, offset)[0]:
                    return
                if state is not None:
                    with self.changed:
                        old = self.status[board][message.antenna_id]
                        if old and old["boot_id"] == state["boot_id"] and (state["sequence"] - old["sequence"]) % (1 << 32) >= (1 << 31):
                            return
                        self.status[board][message.antenna_id] = state
                        self.changed.notify_all()
                return
            offset += size

    def wait(self, board, predicate, timeout):
        """Prefer streamed acknowledgements, recover dropped UDP status via HTTP.

        Never hold the collector lock during a control request: stream reception
        must remain able to make progress while that request is in flight.
        """
        deadline = time.monotonic() + timeout
        next_poll = min(deadline, time.monotonic() + 0.25)
        state = None
        while True:
            with self.changed:
                sensors = self.status[board]
                if all(s is not None for s in sensors):
                    state = {"sensors": list(sensors)}
                    if predicate(state):
                        return state
                left = next_poll - time.monotonic()
                if left > 0:
                    # Each sensor wakes us separately. Keep collecting streamed
                    # acknowledgements instead of fetching a full JSON status
                    # after every partial update (which strains controller RAM).
                    self.changed.wait(left)
                    continue
            # A missing stream packet does not mean the sensor rejected a command.
            state = _rpc(board, "get_iq_tx")
            if predicate(state):
                return state
            if time.monotonic() >= deadline:
                details = ", ".join(f"{a}: mode={s.get('applied_mode')}, generation={s.get('generation')}, error={s.get('error')}" for a, s in enumerate(state["sensors"]))
                raise TimeoutError(f"{board.host}: sensor settings were not acknowledged ({details})")
            next_poll = min(deadline, time.monotonic() + 0.25)

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
        while self.subscriptions:
            board, subscription = self.subscriptions.pop()
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

    Pilots use REF by default. Select ``antenna_l`` or ``antenna_r`` explicitly
    for over-the-air calibration. Both directions use the selected RF path.
    """
    routes = {"reference": 1, "antenna_r": 2, "antenna_l": 3}
    if pilot_output not in routes:
        raise ValueError(f"pilot_output must be one of {tuple(routes)}")
    if pool._calibrating:
        raise RuntimeError("Calibration is already running")
    boards = pool.boards
    saved_rx = _map(lambda b: b.iq.get_config(), boards)
    saved_tx = _map(lambda b: _rpc(b, "get_iq_tx"), boards)
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
    # Pilot density must not depend on the user's replay length. Short combs
    # leave too few coherent tones on weak inter-board REF links. Pilots use a
    # temporary RX-mode buffer; the original source is restored afterwards.
    period = 1024
    bins = np.array([k for k in range(-period * 3 // 16, period * 3 // 16 + 1) if abs(k) >= 8])
    owners = np.where(abs(bins) == period // 16 + 1, 0, abs(bins) % 8)
    frequencies = bins * 80e6 / period
    target_ids = [8 * bi + a for bi, a in targets]
    positions = [(bi, *boards[bi].revision.antenna_id_to_row_col(a)) for bi, a in targets]
    zero = {"phase_deg": [0] * 8, "delay_samples": [0] * 8}
    pilots = set()
    monitors = _map(lambda b: _rpc(b, "get_iq_monitor"), boards)
    capture = _Captures(boards)
    success = False
    try:

        def prepare(b):
            _rpc(b, "set_iq_monitor", {"enabled": False})
            _rpc(b, "set_iq_tx", {"muted": True, "modes": ["iq_rx"] * 8, "calibration": zero, "steering": zero, "rf_gain": {"a": 0, "b": 0}, "bandwidth_mhz": 40})

        _map(prepare, boards)
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
                "rf_switch": routes[pilot_output],
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
        boot_ids = [[x["boot_id"] for x in state["sensors"]] for state in saved_tx]
        count = len(targets)
        response = np.zeros((len(bins), count, count), complex)
        valid = np.zeros(response.shape, bool)
        # Two reference sensors provide redundant reciprocal paths. A whole
        # board's remaining sensors transmit orthogonal combs in one capture.
        hubs = [0]
        if count > 1:
            hubs.append(next((i for i, (bi, _) in enumerate(targets) if bi != targets[0][0]), 1))
        groups = [[i] for i in hubs]
        groups += [[i for i, (j, _) in enumerate(targets) if j == bi and i not in hubs] for bi in range(len(boards))]
        groups = [g for g in groups if g]

        def prepare_pilots(bi):
            for group in groups:
                if targets[group[0]][0] != bi:
                    continue
                ants = [targets[i][1] for i in group]
                # Each sensor appears in exactly one group, so all pilots can
                # be prepared before the measurement starts.
                pilots.update((bi, a) for a in ants)
                # Dense reference pilots spread power across eight times as
                # many tones. Keep them strong for distant receivers, but
                # reduce simultaneous OTA pilots to avoid clipping nearby RX.
                pilot_amplitude = 0.1
                if len(boards) > 1:
                    if pilot_output == "reference":
                        pilot_amplitude = 0.4
                    elif group[0] in hubs:
                        pilot_amplitude = 0.2
                _waveform(
                    boards[bi],
                    {
                        "type": "calibration",
                        "samples": period,
                        "amplitude": pilot_amplitude,
                        "pilot_mask": 255 if group[0] in hubs else 0,
                        "antennas": ants,
                    },
                    timeout,
                    wait=capture.wait,
                    retry=True,
                )

        _map(prepare_pilots, range(len(boards)))
        for group in groups:
            bi = targets[group[0]][0]
            board = boards[bi]
            ants = [targets[i][1] for i in group]
            receiving = [i for i in range(count) if i not in group] if group[0] in hubs else hubs
            pool._logger.info("TX calibration: simultaneous sensors %s/%s", board.host, ants)
            _rpc(
                board,
                "set_iq_tx",
                {
                    "muted": False,
                    "transmitters": [{"antid": a, "output": pilot_output, "amplitude": 1} for a in ants],
                    "modes": ["iq_tx" if a in ants else "iq_rx" for a in range(8)],
                },
            )
            # Mode/enable status can precede the first synchronized replay.
            # Wait for the origin too instead of rejecting that transient state.
            state = capture.wait(
                board,
                lambda s: all(s["sensors"][a]["enabled"] and s["sensors"][a]["applied_mode"] == "iq_tx" and s["sensors"][a].get("origin_ns", 0) for a in ants),
                timeout,
            )
            origins = [state["sensors"][a].get("origin_ns", 0) for a in ants]
            earliest = {target_ids[rx]: max(int(reference[rx]) + origin - int(reference[tx]) for tx, origin in zip(group, origins)) + period * 12.5 for rx in receiving}
            frames = capture.collect(list(earliest), period, 8, timeout, not_before=earliest)
            # One FFT per receiver capture separates all simultaneous pilots.
            for rx in receiving:
                spectra = []
                for frame in frames[target_ids[rx]]:
                    z = np.concatenate([p.decode_iq() for p in frame])
                    if max(abs(z.real).max(), abs(z.imag).max()) >= 500:
                        continue
                    p = frame[0]
                    if any(q.fire_time_ns != p.fire_time_ns for q in frame):
                        continue
                    delta_ns = p.fire_time_ns - int(reference[rx]) + (p.chunk_counter % (period // 256)) * 3200
                    delta = ((delta_ns + period * 6.25) % (period * 12.5) - period * 6.25) * 1e-9
                    rx_phase = phases[rx] + 2 * np.pi * frequencies * (-timing[rx] + delta) + 2 * np.pi * (center + frequencies) * paths[rx]
                    spectra.append(np.fft.fft(z)[bins % period] / period * np.exp(-1j * rx_phase))
                if len(spectra) < 8:
                    continue
                spectra = np.array(spectra)
                mean = spectra.mean(axis=0)
                coherent = abs(mean) ** 2 > 0.9 * np.mean(abs(spectra) ** 2, axis=0)
                for slot, (tx, origin) in enumerate(zip(group, origins)):
                    selected = np.ones(len(bins), bool) if tx in hubs else owners == slot
                    latency = ((origin - int(reference[tx]) + period * 6.25) % (period * 12.5) - period * 6.25) * 1e-9
                    response[:, rx, tx] = mean * np.exp(2j * np.pi * frequencies * latency)
                    valid[:, rx, tx] = selected & coherent
            check = capture.wait(board, lambda s: True, timeout)
            if [s["boot_id"] for s in check["sensors"]] != boot_ids[bi] or any(check["sensors"][a]["origin_ns"] != origin for a, origin in zip(ants, origins)):
                raise RuntimeError("A transmitter restarted during calibration")
            _rpc(board, "set_iq_tx", {"muted": True, "modes": ["iq_rx"] * 8})
            capture.wait(board, lambda s: all(x["applied_mode"] == "iq_rx" for x in s["sensors"]), timeout)
        phase, delay, quality = fit_links(frequencies, response, valid)
        if quality["phase_residual_deg"] > 5 or quality["delay_residual_samples"] > 0.3:
            raise RuntimeError(f"Reciprocal calibration did not validate: {quality}")

        def restore(pair):
            bi, b = pair
            capture.wait(b, lambda s: all(x["applied_mode"] == "iq_rx" for x in s["sensors"]), timeout)
            ants = [a for j, a in pilots if j == bi]
            if ants:
                _waveform(b, {"type": "restore", "samples": length, "antennas": ants}, timeout, wait=capture.wait, retry=True)
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

        _map(restore, enumerate(boards))
        final = _map(
            lambda pair: capture.wait(
                pair[1],
                lambda s: all(
                    x["applied_mode"] == saved_rx[pair[0]]["modes"][a] and (x["applied_mode"] != "iq_tx" or x["enabled"] and x.get("origin_ns")) for a, x in enumerate(s["sensors"])
                ),
                timeout,
            ),
            enumerate(boards),
        )
        latency = np.array(
            [((final[bi]["sensors"][a]["origin_ns"] - int(reference[i]) + length * 6.25) % (length * 12.5) - length * 6.25) / 12.5 for i, (bi, a) in enumerate(targets)]
        )
        delay -= latency - latency[0]
        corrections = [copy.deepcopy(s["calibration"]) for s in saved_tx]
        for i, (bi, a) in enumerate(targets):
            corrections[bi]["phase_deg"][a] = float(phase[i])
            corrections[bi]["delay_samples"][a] = float(delay[i])

        def apply(pair):
            bi, b = pair
            _rpc(b, "set_iq_tx", {"calibration": corrections[bi]})
            ants = [a for j, a in targets if j == bi]
            state = capture.wait(
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

        _map(apply, enumerate(boards))
        # Retain valid RX calibration when restoring a lower receive sample rate.
        generations = _map(lambda b: b.iq.get_config()["config_generation"], boards)
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

        def resume(pair):
            bi, b = pair
            b.iq.set_config({"trigger": saved_rx[bi]["trigger"]})
            _rpc(b, "set_iq_tx", {"muted": saved_tx[bi]["muted"]})

        _map(resume, enumerate(boards))
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
                        _wait(b, lambda state: all(state["sensors"][a]["applied_mode"] == "iq_rx" for a in ants), timeout)
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
