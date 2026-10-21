"""TX calibration using reciprocal links between sensors.

Configure IQ TX roles and source waveforms before calling ``IQReceiver.calibrate_tx``.
The boards must share their sample clock and REF source. Calibration temporarily
routes pilots through REF, with the antennas disconnected. Signals travel
between splitter outputs rather than backwards through the amplifier to the
controller. Their propagation response cancels in the two-way ratios.
"""

import copy
import time
from collections import deque
from .iq_reference import _map
from .iq_tx_reciprocity_fit import DisconnectedLinks, additional_reference, fit_links, validate_links
from .iq_tx_control import TXStatus, _rpc, _wait, _waveform

from .iq_packet import IQChunkPacket, IQ_CHUNK_TYPE_HEADER, IQ_CHUNK_FLAG_REFERENCE, IQ_CHUNK_FLAG_REFERENCE_READY

import numpy as np


class _Captures(TXStatus):
    """Bounded collector for consecutive chunks, independent of complete RX pools."""

    def __init__(self, boards):
        super().__init__(boards)
        self.ids = set()
        self.pending = {}
        self.frames = {}
        self.chunks = 1
        self.generations = {}
        self.not_before = {}
        try:
            for bi, b in enumerate(boards):
                self.subscriptions.append((b, b.subscribe_sensor_messages(IQ_CHUNK_TYPE_HEADER, lambda m, bi=bi: self.receive(8 * bi + m.antenna_id, m))))
        except Exception:
            self.close()
            raise

    def receive(self, sensor, message):
        if sensor not in self.ids:
            return
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
            rx_gain = 42 if pilot_output != "reference" or pool.master_board.wifi_rx.get_config()["calib-source"] else 30
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
            # REF onset detection uses its own path-dependent gain.
            # The pilot receiver gain is restored by sync before measurements.
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
        validation = np.zeros_like(response)
        validation_valid = np.zeros_like(valid)
        # Two reference sensors provide redundant reciprocal paths. A whole
        # board's remaining sensors transmit orthogonal combs in one capture.
        hubs = [0]
        if count > 1:
            hubs.append(next((i for i, (bi, _) in enumerate(targets) if bi != targets[0][0]), 1))
        groups = [[i] for i in hubs]
        groups += [[i for i, (j, _) in enumerate(targets) if j == bi and i not in hubs] for bi in range(len(boards))]
        groups = [g for g in groups if g]

        def prepare_group(group, dense):
            bi = targets[group[0]][0]
            ants = [targets[i][1] for i in group]
            pilots.update((bi, a) for a in ants)
            # Dense pilots spread their power over eight times as many tones.
            amplitude = 0.1
            if len(boards) > 1:
                amplitude = 0.4 if pilot_output == "reference" else (0.2 if dense else 0.1)
            _waveform(
                boards[bi],
                {"type": "calibration", "samples": period, "amplitude": amplitude, "pilot_mask": 255 if dense else 0, "antennas": ants},
                timeout,
                wait=capture.wait,
            )

        def prepare_pilots(bi):
            for group in groups:
                if targets[group[0]][0] == bi:
                    prepare_group(group, group[0] in hubs)

        _map(prepare_pilots, range(len(boards)))

        def measure(group, dense):
            bi = targets[group[0]][0]
            board = boards[bi]
            ants = [targets[i][1] for i in group]
            receiving = [i for i in range(count) if i not in group]
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
            repeats = 8
            frames = capture.collect(list(earliest), period, 2 * repeats, timeout, not_before=earliest)
            # Disjoint captures fit and validate the same fixed corrections.
            # Keep rejected captures in their original split to avoid overlap.
            for rx in receiving:
                spectra = [[], []]
                for frame_index, frame in enumerate(frames[target_ids[rx]]):
                    z = np.concatenate([p.decode_iq() for p in frame])
                    if max(abs(z.real).max(), abs(z.imag).max()) >= 500:
                        continue
                    p = frame[0]
                    if any(q.fire_time_ns != p.fire_time_ns for q in frame):
                        continue
                    delta_ns = p.fire_time_ns - int(reference[rx]) + (p.chunk_counter % (period // 256)) * 3200
                    delta = ((delta_ns + period * 6.25) % (period * 12.5) - period * 6.25) * 1e-9
                    rx_phase = phases[rx] + 2 * np.pi * frequencies * (-timing[rx] + delta) + 2 * np.pi * (center + frequencies) * paths[rx]
                    spectra[frame_index >= repeats].append(np.fft.fft(z)[bins % period] / period * np.exp(-1j * rx_phase))
                for samples, output, accepted in zip(spectra, (response, validation), (valid, validation_valid)):
                    # A later dense pilot replaces earlier sparse observations.
                    for tx in group:
                        accepted[:, rx, tx] = False
                    if len(samples) < repeats:
                        continue
                    samples = np.array(samples)
                    mean = samples.mean(axis=0)
                    coherent = abs(mean) ** 2 > 0.9 * np.mean(abs(samples) ** 2, axis=0)
                    for slot, (tx, origin) in enumerate(zip(group, origins)):
                        selected = np.ones(len(bins), bool) if dense else owners == slot
                        latency = ((origin - int(reference[tx]) + period * 6.25) % (period * 12.5) - period * 6.25) * 1e-9
                        output[:, rx, tx] = mean * np.exp(2j * np.pi * frequencies * latency)
                        accepted[:, rx, tx] = selected & coherent
            check = capture.wait(board, lambda s: True, timeout)
            if [s["boot_id"] for s in check["sensors"]] != boot_ids[bi] or any(check["sensors"][a]["origin_ns"] != origin for a, origin in zip(ants, origins)):
                raise RuntimeError("A transmitter restarted during calibration")
            _rpc(board, "set_iq_tx", {"muted": True, "modes": ["iq_rx"] * 8})
            capture.wait(board, lambda s: all(x["applied_mode"] == "iq_rx" for x in s["sensors"]), timeout)

        for group in groups:
            measure(group, group[0] in hubs)
        extra_references = 0
        while True:
            try:
                phase, delay, quality = fit_links(frequencies, response, valid)
                checked = validate_links(frequencies, validation, validation_valid, phase, delay)
                break
            except DisconnectedLinks as error:
                # Usually no extra measurements are needed. If a fixed pilot
                # cannot reach a sensor, complete reciprocal links elsewhere.
                candidate = additional_reference(error.components, hubs, valid & validation_valid)
                if candidate is None or extra_references >= 2:
                    raise
                pool._logger.info("TX calibration: adding reference sensor %s", targets[candidate])
                hubs.append(candidate)
                prepare_group([candidate], True)
                measure([candidate], True)
                # Its former simultaneous peers could not listen to it. Send
                # their pilots again with the new reference now receiving.
                peers = next((g for g in groups if candidate in g), [])
                peers = [i for i in peers if i not in hubs]
                if peers:
                    prepare_group(peers, False)
                    measure(peers, False)
                extra_references += 1
        quality.update({"validation_" + key: value for key, value in checked.items()})
        quality["additional_references"] = extra_references
        if any(q["phase_residual_deg"] > 5 or q["delay_residual_samples"] > 0.3 for q in (quality, checked)):
            raise RuntimeError(f"Reciprocal calibration did not validate: {quality}")

        def restore(pair):
            bi, b = pair
            capture.wait(b, lambda s: all(x["applied_mode"] == "iq_rx" for x in s["sensors"]), timeout)
            ants = [a for j, a in pilots if j == bi]
            if ants:
                _waveform(b, {"type": "restore", "samples": length, "antennas": ants}, timeout, wait=capture.wait)
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
                lambda s: all(x["applied_mode"] == saved_rx[pair[0]]["modes"][a] and (x["applied_mode"] != "iq_tx" or x["enabled"] and x.get("origin_ns")) for a, x in enumerate(s["sensors"])),
                timeout,
            ),
            enumerate(boards),
        )
        latency = np.array([((final[bi]["sensors"][a]["origin_ns"] - int(reference[i]) + length * 6.25) % (length * 12.5) - length * 6.25) / 12.5 for i, (bi, a) in enumerate(targets)])
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
        return {"calibration": corrections, **quality, "_center_hz": center, "_tx_states": {(bi, a): (final[bi]["sensors"][a]["boot_id"], final[bi]["sensors"][a]["epoch"]) for bi, a in targets}}
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
