"""Buffered, synchronized low-bandwidth sources over the existing TX scene."""

import math
import secrets
import struct
import threading
import time

import numpy as np

from .calibration import Antenna, CalibrationState
from .iq_tx_control import _rpc
from .steering import Beamspace, Weights
from .waveforms import CW


class IQStream:
    """One streamed source, weighted independently on every selected antenna.

    Use ``Pool.tx.open_stream()``. CS8 accepts interleaved signed I8/Q8 bytes,
    an (N, 2) int8 array, or normalized complex samples. Single-carrier mode
    accepts one byte per constellation index and packs indices on the wire;
    pulse shaping runs on the sensors. An input underrun becomes silence. A
    subsequent write prefills a new session and rejoins the shared symbol grid.

    Existing cyclic beams remain additive contributions. Their configuration,
    RF settings and calibration must stay fixed until close(). Streaming
    currently requires synchronized 16 MSa/s replay and a prepared waveform.
    """

    RATE = 16_000_000
    CAPACITY = 65536

    def __init__(self, transmitter, *, mode="cs8", sample_rate_hz=16000, amplitude=0.1, antennas=None, steering=None, modulation=None):
        self.transmitter = transmitter
        self.closed = False
        self.started = self.finished = False
        self.offset = 0
        self._auto_start = False
        self._phase_origin = None
        self.resumptions = 0
        self.previous_missing = 0
        self.session = secrets.randbits(32) or 1
        # Recovery reinitializes the session while write() still owns this
        # lock. Keep the same lock so concurrent callers remain serialized.
        if not hasattr(self, "_lock"):
            self._lock = threading.RLock()
        self.targets = []
        self._feedback = {}
        self._subscriptions = []
        self._next_send = {}
        self._feedback_changed = threading.Condition()
        self.modulation = modulation
        if mode not in ("cs8", "single_carrier"):
            raise ValueError("mode must be cs8 or single_carrier")
        if mode == "cs8" and sample_rate_hz not in (8000, 16000, 32000, 40000, 50000):
            raise ValueError("CS8 sample rate must be 8000, 16000, 32000, 40000 or 50000 Sa/s")
        if not math.isfinite(amplitude) or not 0 <= amplitude <= 1:
            raise ValueError("amplitude must lie between 0 and 1")
        if steering is not None and not isinstance(steering, (Beamspace, Weights)):
            raise TypeError("steering must be Beamspace, Weights, or None")
        from .stream_sources import SingleCarrier

        if mode == "single_carrier" and not isinstance(modulation, SingleCarrier):
            raise ValueError("single_carrier mode requires a SingleCarrier configuration")
        if mode == "cs8" and modulation is not None:
            raise ValueError("Raw IQ does not take a modulation table")
        self.mode = mode
        self.steering = steering
        self.sample_rate_hz = int(sample_rate_hz) if mode == "cs8" else modulation.symbol_rate_hz
        self.stride = 2 if mode == "cs8" else 1
        self.capacity = self.CAPACITY if mode == "cs8" else 2 * self.CAPACITY
        self.bits = max(1, (len(modulation.constellation) - 1).bit_length()) if mode == "single_carrier" else 16
        self.chunk = 8192 // self.bits
        self._settings = dict(mode=mode, sample_rate_hz=sample_rate_hz, amplitude=amplitude,
                              antennas=antennas, steering=steering, modulation=modulation)
        transmitter.read()
        transmitter.signals._check_beam_calibration()
        self.antennas = transmitter.signals._antennas(antennas)
        self._settings["antennas"] = tuple(self.antennas)
        if isinstance(steering, Beamspace):
            states = [CalibrationState.from_tx_status(a.host, transmitter.states[bi]["sensors"][a.antid]) for bi, b in enumerate(transmitter.boards) for a in self.antennas if a.host == b.host]
            transmitter.owner.calibrations.require_coherent(states)
        try:
            for bi, board in enumerate(transmitter.boards):
                ids = [a.antid for a in self.antennas if a.host == board.host]
                if not ids:
                    continue
                subscription = board.subscribe_controller_messages(0x32535449, lambda data, b=board: self._receive_feedback(b, data))
                self._subscriptions.append((board, subscription))
                state = transmitter.states[bi]
                if any(state["sensors"][a]["applied_mode"] != "iq_tx" or state["sensors"][a]["applied_sample_rate_hz"] != self.RATE or not state["sensors"][a]["samples"] for a in ids):
                    raise ValueError("Prepare a waveform and configure selected sensors for 16 MSa/s IQ TX first")
                if any(state["sensors"][a]["samples"] > 2048 for a in ids):
                    raise ValueError("Streaming requires a replay length of at most 2048 samples")
                weights, delays = np.zeros(8, complex), np.zeros(8)
                for a in ids:
                    antenna = Antenna(board.host, a)
                    if isinstance(steering, Beamspace):
                        position = None if transmitter.positions is None else transmitter.positions[bi, a]
                        weights[a], delays[a] = steering.coefficients(position, state["sensors"][a]["frequency_hz"], CW(), self.RATE)
                    elif isinstance(steering, Weights):
                        weights[a] = steering.values.get(antenna, 1)
                        delays[a] = steering.delays_s.get(antenna, 0) * self.RATE
                    else:
                        weights[a] = 1
                weights *= amplitude
                if not np.all(np.isfinite(weights)) or not np.all(np.isfinite(delays)) or np.any(abs(weights) > 1.000001) or np.any(abs(delays) > 8192):
                    raise ValueError("Stream weights must have magnitude ≤1 and delays within ±8192 replay samples")
                target = (board, ids, tuple(state["sensors"][a]["boot_id"] for a in ids))
                self.targets.append(target)
                payload = struct.pack("<4I24f", 1 if mode == "cs8" else 2, int(sample_rate_hz) if mode == "cs8" else 0, 1, len(modulation.payload) if modulation else 0, *weights.real, *weights.imag, *delays)
                self._send(target, 1, payload=payload)
            self._wait(lambda states: all(s.get("session") == self.session and s["state"] == "buffering" for s in states), 5, opening=True)
            if modulation:
                for offset in range(0, len(modulation.payload), 1024):
                    chunk = modulation.payload[offset : offset + 1024]
                    for target in self.targets:
                        self._send(target, 6, offset, len(chunk), chunk)
                    self._wait(lambda states: all(s["table_received"] >= offset + len(chunk) for s in states), 5)
        except BaseException as error:
            self._abort(error)
            raise

    def _send(self, target, command, offset=0, units=0, payload=b""):
        board, ids, _ = target
        header = struct.pack("<IBBBBIII", 0x31535449, 3, command, sum(1 << a for a in ids), 0, self.session, offset, units)
        if command in (2, 4):
            now = time.monotonic()
            time.sleep(max(0, self._next_send.get(board, now) - now))
            board.send_stream_data(header + payload)
            # Small live blocks also need a packet-rate bound when a file or
            # pipe can supply them faster than the configured source clock.
            spacing = max(0.002, (len(header) + len(payload) + 32) / 220000)
            self._next_send[board] = time.monotonic() + spacing
            return None
        return board.control.fetch("iq_tx_stream", header + payload)

    def _receive_feedback(self, board, data):
        if len(data) != 728:
            return
        words = struct.unpack("<182I", data)
        if words[:2] != (0x32535449, 3):
            return
        names = (
            "session",
            "state",
            "error",
            "accepted",
            "consumed",
            "end",
            "format",
            "input_rate_hz",
            "capacity",
            "rejected",
            "rendered_blocks",
            "max_render_us",
            "max_late_us",
            "clipped",
            "sequence",
            "max_source_us",
            "max_expand_us",
            "missing",
            "late",
            "table_received",
        )
        sensors = []
        for a in range(8):
            pos = 6 + 22 * a
            state = dict(zip(names, words[pos + 2 : pos + 22]))
            state.update(antid=a, boot_id=words[pos], available=bool(words[pos + 1]))
            state["state"] = ("closed", "buffering", "armed", "running", "drained", "fault", "starved")[state["state"]] if state["state"] < 7 else "unknown"
            if state["end"] == 0xFFFFFFFF:
                state["end"] = None
            sensors.append(state)
        with self._feedback_changed:
            old = self._feedback.get(board)
            if old and not 0 < (words[2] - old[2]) % 2**32 < 2**31:
                return
            self._feedback[board] = (time.monotonic(), sensors, words[2], words[3:6])
            self._feedback_changed.notify_all()

    def _states(self, *, opening=False, allow_fault=False):
        result = []
        for board, ids, boots in self.targets:
            with self._feedback_changed:
                deadline = time.monotonic() + 3
                while True:
                    feedback = self._feedback.get(board)
                    if feedback and time.monotonic() - feedback[0] < 0.5:
                        break
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError("Stream feedback is stale; uploads stopped")
                    self._feedback_changed.wait(min(left, 0.1))
                states = feedback[1]
            for a, boot in zip(ids, boots):
                state = states[a]
                if not state["available"] or state["boot_id"] != boot:
                    raise RuntimeError(f"{board.host}/{a}: sensor restarted or streaming status disappeared")
                if not opening and state["session"] != self.session:
                    raise RuntimeError(f"{board.host}/{a}: stream session changed")
                if not allow_fault and state["session"] == self.session and (state["state"] == "fault" or state["error"]):
                    detail = {0x106: "unsupported scene/rate", 0x107: "replay refill deadline missed", 0x10C: "input FIFO underrun"}.get(state["error"], "stream configuration/state error")
                    raise RuntimeError(f"{board.host}/{a}: {detail} ({state['error']:#x})")
                result.append(dict(state))
        return result

    def _wait(self, predicate, timeout, *, opening=False):
        self._timeout(timeout)
        deadline = time.monotonic() + timeout
        while True:
            states = self._states(opening=opening)
            if predicate(states):
                return states
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for sensor stream acknowledgement")
            time.sleep(0.02)

    def status(self):
        """Return sensor counters, or the last received snapshot after close()."""
        with self._lock:
            if self.closed:
                with self._feedback_changed:
                    return [dict(self._feedback[board][1][a]) for board, ids, _ in self.targets if board in self._feedback for a in ids]
            return self._states()

    def transport_status(self):
        """Return controller counters from the existing downlink reports.

        Counters are cumulative since controller boot. Sensor-specific missing
        units and FIFO credit are available through :meth:`status`.
        """
        with self._lock:
            if not self.closed:
                self._states()
            with self._feedback_changed:
                return {
                    board.host: dict(
                        sequence=self._feedback[board][2],
                        packets=self._feedback[board][3][0],
                        rejected=self._feedback[board][3][1],
                        last_error=self._feedback[board][3][2],
                    )
                    for board, _, _ in self.targets
                }

    def _encode(self, samples):
        if isinstance(samples, (bytes, bytearray, memoryview)):
            data = bytes(samples)
        elif self.mode == "single_carrier":
            a = np.asarray(samples)
            if a.ndim != 1 or not np.all(np.isfinite(a)) or np.any(a < 0) or np.any(a >= len(self.modulation.constellation)) or np.any(a != np.floor(a)):
                raise ValueError("Input must contain valid constellation indices")
            data = a.astype(np.uint8).tobytes()
        else:
            a = np.asarray(samples)
            if a.dtype == np.int8 and a.ndim == 2 and a.shape[1] == 2:
                data = a.tobytes()
            elif a.ndim == 1 and np.iscomplexobj(a):
                if not np.all(np.isfinite(a)) or np.any(abs(a.real) > 1) or np.any(abs(a.imag) > 1):
                    raise ValueError("Complex IQ components must be finite and within [-1, 1]")
                data = (np.column_stack((a.real, a.imag)) * 127).round().astype(np.int8).tobytes()
            else:
                raise ValueError("Use complex samples, an (N, 2) int8 array, or interleaved CS8 bytes")
        if len(data) % self.stride or (self.mode == "single_carrier" and any(b >= len(self.modulation.constellation) for b in data)):
            raise ValueError("Invalid sample/index encoding")
        return data

    def write(self, samples, *, timeout=10):
        """Send samples/indices subject to the slowest sensor's FIFO credit.

        Return after transport submission, not delivery acknowledgement. UDP
        losses become zero-valued source units without retransmission.
        Before start(), prefill at least 1024 IQ samples or four indices.
        After an underrun, new writes buffer a replacement session. Old packets
        are discarded; resumption is scheduled on a common symbol boundary.
        """
        with self._lock:
            if self.closed or self.finished:
                raise RuntimeError("Stream is closed or already finished")
            data = self._encode(samples)
            count = len(data) // self.stride
            if not count:
                return 0
            if self.started and any(s["state"] == "starved" for s in self._states()):
                self._recover()
            begin, end = self.offset, self.offset + count
            if end > 0x7FFFFFFF:
                raise ValueError("Stream exceeds the protocol's sample counter range")
            self._timeout(timeout)
            try:
                if not self.started and not self._auto_start and end > min(s["capacity"] for s in self._states()):
                    raise ValueError("Prefill exceeds FIFO capacity; start before writing more")
                return self._write_data(data, begin, end, count, timeout)
            except BaseException as error:
                self._abort(error)
                raise

    def _write_data(self, data, begin, end, count, timeout):
        deadline = time.monotonic() + timeout
        sent = begin
        while sent < end:
            states = self._states()
            if self.started and any(s["state"] == "starved" for s in states):
                self._recover()
                self.write(data[(sent - begin) * self.stride:], timeout=max(.1, deadline - time.monotonic()))
                return count
            consumed = min(s["consumed"] for s in states)
            capacity = min(s["capacity"] for s in states)
            prefill = min(capacity, max(1024 if self.mode == "cs8" else 4, math.ceil(self.sample_rate_hz * .1)))
            limit = min(end, consumed + capacity, sent + 8 * self.chunk)
            if self._auto_start and not self.started:
                limit = min(limit, prefill)
                if sent >= prefill:
                    self.offset = sent
                    self.start()
                    self._auto_start = False
                    continue
            # Do not fragment bulk writes whenever a small credit update
            # arrives. Wait for one full payload unless this is the write's
            # final/live fragment or a recovery prefill boundary.
            if limit < end and not (self._auto_start and not self.started):
                limit = sent + (limit - sent) // self.chunk * self.chunk
            if time.monotonic() >= deadline:
                raise TimeoutError("Stream write timed out waiting for FIFO credit")
            if sent < limit:
                self._send_data(data, begin, sent, limit)
                sent = limit
            else:
                time.sleep(0.005)
        self.offset = end
        if self._auto_start and not self.started and end >= prefill:
            self.start()
            self._auto_start = False
        return count

    def _send_data(self, data, begin, offset, end):
        while offset < end:
            units = min(self.chunk, end - offset)
            payload = data[(offset - begin) * self.stride : (offset - begin + units) * self.stride]
            if self.mode == "single_carrier":
                indices = np.frombuffer(payload, dtype=np.uint8)
                bits = ((indices[:, None] >> np.arange(self.bits, dtype=np.uint8)) & 1).reshape(-1)
                payload = np.packbits(bits, bitorder="little").tobytes()
            for target in self.targets:
                self._send(target, 2, offset, units, payload)
            offset += units

    def _recover(self):
        """Discard stale packets and rejoin the original symbol grid together.

        CLOSE keeps cyclic replay running. OPEN resets only the source FIFO;
        ARM is sent after every sensor acknowledges the replacement prefill.
        """
        settings = self._settings
        origin, resumptions = self._phase_origin, self.resumptions
        missing = self.previous_missing + max((s["missing"] for s in self._states()), default=0)
        self.close()
        self.__init__(self.transmitter, **settings)
        self._phase_origin = origin
        self.resumptions = resumptions + 1
        self.previous_missing = missing
        self._auto_start = True

    def start(self, *, lead_time=0.5):
        """Arm a common future source origin on the calibrated replay grid.

        Unmute the prepared scene first. The reader joins the streaming ring
        without restarting, preserving the stored scene's calibration.
        """
        with self._lock:
            if self.closed or self.started:
                raise RuntimeError("Stream is closed or already started")
            if not math.isfinite(lead_time) or lead_time < 0.1:
                raise ValueError("lead_time must be finite and at least 0.1 seconds")
            if self.offset < (1024 if self.mode == "cs8" else 4):
                raise ValueError("Prefill at least 1024 IQ samples or four constellation indices")
            self._wait(lambda states: all(s["accepted"] >= (1024 if self.mode == "cs8" else 4) for s in states), 3)
            plans, sequences, positions = [], set(), []
            for target in self.targets:
                board, ids, _ = target
                state = _rpc(board, "get_iq_tx")
                self.transmitter.states[self.transmitter.boards.index(board)] = state
                if state["muted"]:
                    raise ValueError("Unmute the prepared TX scene before starting its stream")
                sensors = state["sensors"]
                for a in ids:
                    if not sensors[a]["enabled"] or not sensors[a]["sync_seq"]:
                        raise ValueError("Streaming requires running TX on a synchronized sample grid")
                    sequences.add(sensors[a]["sync_seq"])
                    positions.append(sensors[a]["sample_position"])
                plans.append((target, [s["epoch"] for s in sensors]))
            self.transmitter.signals._check_beam_calibration()
            if isinstance(self.steering, Beamspace):
                states = [CalibrationState.from_tx_status(a.host, self.transmitter.states[bi]["sensors"][a.antid]) for bi, b in enumerate(self.transmitter.boards) for a in self.antennas if a.host == b.host]
                self.transmitter.owner.calibrations.require_coherent(states)
            if len(sequences) != 1:
                raise ValueError("Selected sensors do not share a synchronization sequence")
            self.start_sample = int(max(positions)) + math.ceil(lead_time * self.RATE)
            self.start_phase_q8 = 0
            period = round(4096000000 / self.sample_rate_hz)
            if self._phase_origin is not None:
                symbol = max(0, (self.start_sample * 256 - self._phase_origin + period - 1) // period)
                origin = self._phase_origin + symbol * period
                self.start_sample = (origin + 255) // 256
                self.start_phase_q8 = self.start_sample * 256 - origin
            else:
                self._phase_origin = self.start_sample * 256
            if self.start_sample >= 2**53:
                raise ValueError("Scheduled start exceeds the sample-clock range")
            try:
                for target, epochs in plans:
                    self._send(target, 3, payload=struct.pack("<QI8II", self.start_sample, next(iter(sequences)), *epochs, self.start_phase_q8))
                self._wait(lambda states: all(s["state"] in ("running", "drained", "starved") for s in states), lead_time)
            except BaseException as error:
                self._abort(error)
                raise
            self.started = True
            return self.start_sample

    def finish(self):
        """Mark the exact source end; subsequent output is silence plus static beams."""
        with self._lock:
            if self.closed:
                raise RuntimeError("Stream is closed")
            try:
                if self._auto_start and not self.started:
                    self.start()
                    self._auto_start = False
                # Queue after the final DATA on the same transport. Waiting for
                # all sensor snapshots can exhaust the FIFO at high rates.
                for target in self.targets:
                    self._send(target, 4, self.offset)
                self._wait(lambda states: all(s["end"] == self.offset for s in states), 5)
                self.finished = True
            except BaseException as error:
                self._abort(error)
                raise

    def wait(self, *, timeout=10):
        """Wait until a finished stream has drained, retaining the static scene."""
        with self._lock:
            if not self.started or not self.finished:
                raise RuntimeError("Start and finish the stream before waiting for its end")
            return self._wait(lambda states: all(s["state"] == "drained" for s in states), timeout)

    @staticmethod
    def _timeout(timeout):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")

    def _abort(self, error):
        try:
            self.close()
        except Exception as shutdown_error:
            error.add_note(f"Stream shutdown also failed: {shutdown_error}")

    def close(self):
        """Stop the source and restore cyclic replay, waiting for sensor ACKs.

        If acknowledgement fails, mute the board as a fallback and report the
        failure. A partial write can never be reused for different sample data.
        After a replay fault, RF remains stopped until explicitly restarted;
        recalibrate coherent beams before resuming them.
        """
        with self._lock:
            if self.closed:
                return
            errors = []
            for target in self.targets:
                board, ids, boots = target
                try:
                    deadline = time.monotonic() + 5
                    while True:
                        self._send(target, 5)
                        time.sleep(0.05)
                        sensors = self._feedback.get(board, (0, []))[1]
                        if len(sensors) == 8 and all(sensors[a].get("available") and (sensors[a].get("boot_id") != boot or (sensors[a].get("session") == self.session and sensors[a]["state"] == "closed")) for a, boot in zip(ids, boots)):
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Sensor did not acknowledge stream closure")
                except Exception as error:
                    errors.append(error)
                    try:
                        _rpc(board, "set_iq_tx", {"muted": True})
                    except Exception as mute_error:
                        error.add_note(f"Fallback mute also failed: {mute_error}")
            self.closed = True
            for board, subscription in self._subscriptions:
                board.unsubscribe_controller_messages(subscription)
            self._subscriptions.clear()
            if errors:
                raise RuntimeError("Could not confirm every sensor stream stopped; requested fallback mute") from errors[0]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as error:
            if exc is None:
                raise
            exc.add_note(f"Stream shutdown also failed: {error}")
