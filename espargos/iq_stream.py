"""Buffered, synchronized low-bandwidth sources over the existing TX scene."""

import math
import secrets
import struct
import threading
import time

import numpy as np

from .calibration import CalibrationState
from .iq_tx_control import _rpc, estimated_playback_sample
from .iq_stream_protocol import Command
from .stream_sources import Stream
from .steering import Beamspace, Weights


_PAYLOAD_BYTES = 1024
_BEST_EFFORT = 1
_PACKET_HEADER = struct.Struct("<HH")
_OPEN = struct.Struct("<12I")
_ARM = struct.Struct("<QI8II")
_ARM_PREPARE_SECONDS = 0.05  # IQ_TX_STREAM_ARM_LEAD_SAMPLES at 16 MSa/s
_FORMATS = {"cs8": 1, "single_carrier": 2, "gfsk": 3}


class IQStream:
    """One streamed source, weighted independently on every selected antenna.

    Open a live beam with ``beam.open_stream()``. CS8 accepts interleaved signed I8/Q8 bytes,
    an (N, 2) int8 array, or normalized complex samples. Single-carrier mode
    accepts one byte per constellation index and packs indices on the wire;
    pulse shaping runs on the sensors. An input underrun becomes silence. A
    subsequent write prefills a new session and rejoins the shared symbol grid.

    Use tx.schedule_steering() to change directions during playback. Keep
    waveforms, RF settings and calibration fixed until close(). Streaming
    currently requires synchronized 16 MSa/s replay and a prepared waveform.
    """

    RATE = 16_000_000
    CAPACITY = 65536

    def __init__(
        self, transmitter, *, mode="cs8", sample_rate_hz=16000, amplitude=0.1,
        antennas=None, steering=None, modulation=None, packet_gap_us=20000, scene=None,
    ):
        self.transmitter = transmitter
        self._lock = threading.RLock()
        self._phase_origin = None
        self.resumptions = 0
        self.previous_missing = 0
        Stream(mode, sample_rate_hz, modulation, packet_gap_us)
        self.packet_gap_us = packet_gap_us
        self.modulation = modulation
        if not math.isfinite(amplitude) or not 0 <= amplitude <= 1:
            raise ValueError("amplitude must lie between 0 and 1")
        if steering is not None and not isinstance(steering, (Beamspace, Weights)):
            raise TypeError("steering must be Beamspace, Weights, or None")
        self.mode = mode
        self.steering = steering
        self.sample_rate_hz = int(sample_rate_hz) if mode == "cs8" else modulation.symbol_rate_hz
        self.stride = 2 if mode == "cs8" else 1
        self.capacity = self.CAPACITY * {"cs8": 1, "single_carrier": 8, "gfsk": 8}[mode]
        self.bits = max(1, (len(modulation.constellation) - 1).bit_length()) if mode == "single_carrier" else 16
        self.chunk = 8 * _PAYLOAD_BYTES // self.bits
        self._amplitude = amplitude
        self._scene = dict(scene or {})
        self._open_session(antennas)

    def _open_session(self, antennas):
        """Set up a fresh sensor FIFO without resetting the stream's clock history."""
        self.closed = self.started = self.finished = False
        self.offset = 0
        self._auto_start = False
        self.session = secrets.randbits(32) or 1
        self.targets = []
        self._reports = self.transmitter.capture
        self._final_states = []
        transmitter = self.transmitter
        steering = self.steering
        table = self.modulation.payload if self.modulation else b""

        transmitter.read()
        transmitter.signals._check_beam_calibration()
        self.antennas = transmitter.signals._antennas(antennas)
        if isinstance(steering, Beamspace):
            states = [CalibrationState.from_tx_status(a.host, transmitter.states[bi]["sensors"][a.antid]) for bi, b in enumerate(transmitter.boards) for a in self.antennas if a.host == b.host]
            transmitter.owner.calibrations.require_coherent(states)
        try:
            for bi, board in enumerate(transmitter.boards):
                ids = [a.antid for a in self.antennas if a.host == board.host]
                if not ids:
                    continue
                state = transmitter.states[bi]
                if any(state["sensors"][a]["applied_mode"] != "iq_tx" or state["sensors"][a]["applied_sample_rate_hz"] != self.RATE or not state["sensors"][a]["samples"] for a in ids):
                    raise ValueError("Prepare a waveform and configure selected sensors for 16 MSa/s IQ TX first")
                if any(state["sensors"][a]["samples"] > 2048 for a in ids):
                    raise ValueError("Streaming requires a replay length of at most 2048 samples")
                scene_crc = [0] * 8
                for antenna in self.antennas:
                    if antenna.host != board.host:
                        continue
                    sensor = state["sensors"][antenna.antid]
                    identity = (sensor["boot_id"], sensor["crc32"])
                    if self._scene.setdefault(antenna, identity) != identity:
                        raise ValueError(f"{antenna}: stream beam changed since preparation")
                    scene_crc[antenna.antid] = identity[1]
                target = (board, ids, tuple(state["sensors"][a]["boot_id"] for a in ids))
                self.targets.append(target)
                payload = _OPEN.pack(
                    _FORMATS[self.mode], self.sample_rate_hz if self.mode == "cs8" else 0,
                    _BEST_EFFORT, len(table), *scene_crc,
                )
                self._send(target, Command.TXSTREAM_OPEN, payload=payload)
            self._wait(
                lambda s: s.get("session") == self.session and s["state"] == "buffering",
                5, opening=True,
            )
            for offset in range(0, len(table), _PAYLOAD_BYTES):
                chunk = table[offset : offset + _PAYLOAD_BYTES]
                for target in self.targets:
                    self._send(target, Command.TXSTREAM_TABLE, offset, len(chunk), chunk)
                self._wait(lambda s: s["table_received"] >= offset + len(chunk), 5)
        except BaseException as error:
            self._abort(error)
            raise

    def _send(self, target, command, offset=0, units=0, payload=b""):
        board, ids, _ = target
        mask = sum(1 << a for a in ids)
        digital = self.mode != "cs8"
        bit_rate = self.sample_rate_hz * (self.bits if self.mode == "single_carrier" else 1)
        fast = digital and (self.sample_rate_hz > 1_000_000 or bit_rate > 1_600_000)
        spacing = .001 if fast else .002
        if digital and command in (Command.TXSTREAM_DATA, Command.TXSTREAM_PACKET):
            # Unpacking packed indices and GFSK records also costs sensor CPU.
            symbols = _PACKET_HEADER.unpack_from(payload)[0] if command == Command.TXSTREAM_PACKET else units
            spacing = max(spacing, symbols / max(1_100_000, self.sample_rate_hz * 1.1))
        self.transmitter.uplink.send(board, command, mask, self.session, offset, units, payload,
                                     byte_rate=280_000 if fast else 220_000, min_spacing=spacing)

    def _states(self, *, opening=False):
        result = []
        for board, ids, boots in self.targets:
            with self._reports.changed:
                deadline = time.monotonic() + 3
                while True:
                    now = time.monotonic()
                    feedback = {a: self._reports.streams.get((board, a)) for a in ids}
                    if all(item and (item[1]["state"] == "closed" or now - item[0] <
                                     (1.25 if item[1]["state"] in ("drained", "fault", "starved") else .5))
                           for item in feedback.values()):
                        break
                    if now >= deadline:
                        raise TimeoutError("Sensor stream feedback is stale; uploads stopped")
                    self._reports.changed.wait(min(deadline - now, 0.1))
                states = {a: item[1] for a, item in feedback.items()}
            for a, boot in zip(ids, boots):
                state = states[a]
                if not state["available"] or state["boot_id"] != boot:
                    raise RuntimeError(f"{board.host}/{a}: sensor restarted or streaming status disappeared")
                if not opening and state["session"] != self.session:
                    raise RuntimeError(f"{board.host}/{a}: stream session changed")
                if not opening and state["state"] == "closed":
                    raise RuntimeError(f"{board.host}/{a}: stream closed on sensor")
                if state["session"] == self.session and (state["state"] == "fault" or state["error"]):
                    detail = {0x101: "insufficient sensor memory", 0x106: "unsupported scene/rate", 0x107: "stream start or refill deadline missed", 0x10C: "input FIFO underrun"}.get(state["error"], "stream configuration/state error")
                    raise RuntimeError(f"{board.host}/{a}: {detail} ({state['error']:#x})")
                result.append(dict(state))
        return result

    def _wait(self, predicate, timeout, *, opening=False):
        self._timeout(timeout)
        deadline = time.monotonic() + timeout
        while True:
            states = self._states(opening=opening)
            if all(map(predicate, states)):
                return states
            if time.monotonic() >= deadline:
                sensors = (f"{board.host}/{a}" for board, ids, _ in self.targets for a in ids)
                missing = ", ".join(label for label, state in zip(sensors, states) if not predicate(state))
                raise TimeoutError(f"Timed out waiting for sensor stream acknowledgement from {missing}")
            time.sleep(0.02)

    def status(self):
        """Return sensor counters, or the last received snapshot after close()."""
        with self._lock:
            if self.closed:
                return [dict(state) for state in self._final_states]
            return self._states()

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
        invalid_index = (self.mode == "single_carrier" and data and
                         np.frombuffer(data, dtype=np.uint8).max() >= len(self.modulation.constellation))
        if len(data) % self.stride or invalid_index:
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
        if self.mode == "gfsk":
            raise ValueError("Use write_packet() for complete GFSK air packets")
        with self._lock:
            if self.closed or self.finished:
                raise RuntimeError("Stream is closed or already finished")
            data = self._encode(samples)
            count = len(data) // self.stride
            if not count:
                return 0
            if self.started and any(s["state"] == "starved" for s in self._states()):
                self._recover()
            end = self.offset + count
            if end > 0x7FFFFFFF:
                raise ValueError("Stream exceeds the protocol's sample counter range")
            self._timeout(timeout)
            try:
                if not self.started and not self._auto_start and end > min(s["capacity"] for s in self._states()):
                    raise ValueError("Prefill exceeds FIFO capacity; start before writing more")
                self._write_data(data, end, timeout)
                return count
            except BaseException as error:
                self._abort(error)
                raise

    def write_packet(self, packet, *, bit_count=None, gap_us=20000, timeout=10):
        """Submit one complete packet of LSB-first air bits, followed by silence.

        Preamble, access address, whitening and CRC belong to the caller.
        One datagram carries the complete RF packet; loss yields silence.
        No retransmission or fragmentation is performed.
        """
        if self.mode != "gfsk":
            raise ValueError("Packet writes require GFSK mode")
        data = bytes(packet)
        bits = len(data) * 8 if bit_count is None else bit_count
        if (
            not isinstance(bits, int)
            or not 0 <= bits <= 8 * (_PAYLOAD_BYTES - _PACKET_HEADER.size)
            or len(data) != (bits + 7) // 8
        ):
            raise ValueError("Use at most 8160 complete air bits per packet")
        if not math.isfinite(gap_us) or gap_us < 0:
            raise ValueError("Packet gap must be finite and nonnegative")
        units = bits + round(gap_us * self.sample_rate_hz / 1e6)
        if not units or units > self.CAPACITY:
            raise ValueError("Packet plus gap must fit 65536 symbol ticks")
        self._timeout(timeout)
        with self._lock:
            if self.closed or self.finished:
                raise RuntimeError("Stream is closed or finished")
            try:
                deadline = time.monotonic() + timeout
                while True:
                    states = self._states()
                    if self.started and any(s["state"] == "starved" for s in states):
                        self._recover()
                        continue
                    consumed = min(s["consumed"] for s in states)
                    capacity = min(s["capacity"] for s in states)
                    if self.offset + units - consumed <= capacity:
                        break
                    if not self.started:
                        raise ValueError(
                            "Prefill exceeds FIFO capacity; start before writing more"
                        )
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Packet write timed out waiting for FIFO credit")
                    time.sleep(0.002)
                if self.offset + units > 0x7FFFFFFF:
                    raise ValueError("Stream exceeds the symbol counter range")
                payload = _PACKET_HEADER.pack(bits, 0) + data
                for target in self.targets:
                    self._send(target, Command.TXSTREAM_PACKET, self.offset, units, payload)
                self.offset += units
                if self._auto_start and self.offset >= self.capacity // 2:
                    self.start()
            except BaseException as error:
                self._abort(error)
                raise
        return bits

    def _write_data(self, data, end, timeout):
        deadline = time.monotonic() + timeout
        begin = sent = self.offset
        while sent < end:
            states = self._states()
            if self.started and any(s["state"] == "starved" for s in states):
                self._recover()
                data = data[(sent - begin) * self.stride :]
                begin = sent = self.offset
                end = begin + len(data) // self.stride
                deadline = max(deadline, time.monotonic() + .1)
                continue
            consumed = min(s["consumed"] for s in states)
            capacity = min(s["capacity"] for s in states)
            prefill = min(capacity, max(1024 if self.mode == "cs8" else 4, math.ceil(self.sample_rate_hz * .1)))
            limit = min(end, consumed + capacity, sent + 8 * self.chunk)
            if self._auto_start:
                limit = min(limit, prefill)
                if sent >= prefill:
                    self.offset = sent
                    self.start()
                    continue
            # Do not fragment bulk writes whenever a small credit update
            # arrives. Wait for one full payload unless this is the write's
            # final/live fragment or a recovery prefill boundary.
            if limit < end and not self._auto_start:
                limit = sent + (limit - sent) // self.chunk * self.chunk
            if time.monotonic() >= deadline:
                raise TimeoutError("Stream write timed out waiting for FIFO credit")
            if sent < limit:
                self._send_data(data, begin, sent, limit)
                sent = limit
            else:
                time.sleep(0.005)
        self.offset = end
        if self._auto_start and end >= prefill:
            self.start()

    def _send_data(self, data, begin, offset, end):
        while offset < end:
            units = min(self.chunk, end - offset)
            payload = data[(offset - begin) * self.stride : (offset - begin + units) * self.stride]
            if self.mode == "single_carrier":
                indices = np.frombuffer(payload, dtype=np.uint8)
                bits = indices if self.bits == 1 else (
                    (indices[:, None] >> np.arange(self.bits, dtype=np.uint8)) & 1
                ).reshape(-1)
                payload = np.packbits(bits, bitorder="little").tobytes()
            for target in self.targets:
                self._send(target, Command.TXSTREAM_DATA, offset, units, payload)
            offset += units

    def _recover(self):
        """Discard stale packets and rejoin the original symbol grid together.

        CLOSE keeps cyclic replay running. OPEN resets only the source FIFO;
        ARM is sent after every sensor acknowledges the replacement prefill.
        """
        missing = max((s["missing"] for s in self._states()), default=0)
        self.close()
        self._open_session(self.antennas)
        self.resumptions += 1
        self.previous_missing += missing
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
            # Prepare only after the complete prefill has crossed UART. Table
            # construction can occupy the replay owner long enough to overflow
            # UART RX if it starts while a large prefill is still in flight.
            self._wait(lambda s: s["accepted"] >= self.offset, 3)
            plans, sequences, positions = [], set(), []
            for target in self.targets:
                board, ids, _ = target
                read_started = time.monotonic()
                state = _rpc(board, "get_iq_tx")
                self.transmitter.states[self.transmitter.boards.index(board)] = state
                if state["muted"]:
                    raise ValueError("Unmute the prepared TX scene before starting its stream")
                sensors = state["sensors"]
                for a in ids:
                    if not sensors[a]["enabled"] or not sensors[a]["sync_seq"]:
                        raise ValueError("Streaming requires running TX on a synchronized sample grid")
                    sequences.add(sensors[a]["sync_seq"])
                    positions.append((estimated_playback_sample(sensors[a]), read_started))
                plans.append((target, [s["epoch"] for s in sensors]))
            self.transmitter.signals._check_beam_calibration()
            if isinstance(self.steering, Beamspace):
                states = [CalibrationState.from_tx_status(a.host, self.transmitter.states[bi]["sensors"][a.antid]) for bi, b in enumerate(self.transmitter.boards) for a in self.antennas if a.host == b.host]
                self.transmitter.owner.calibrations.require_coherent(states)
            if len(sequences) != 1:
                raise ValueError("Selected sensors do not share a synchronization sequence")
            # Each sensor needs 50 ms after ARM arrives to prepare its banks.
            # Leave the requested delivery time in addition to that interval.
            now = time.monotonic()
            self.start_sample = max(
                position + math.ceil((now - read_started + lead_time + _ARM_PREPARE_SECONDS) * self.RATE)
                for position, read_started in positions
            )
            self.start_phase_q8 = 0
            period = round(4096000000 / self.sample_rate_hz)
            if self._phase_origin is not None:
                symbol = max(0, (self.start_sample * 256 - self._phase_origin + period - 1) // period)
                origin = self._phase_origin + symbol * period
                self.start_sample = (origin + 255) // 256
                self.start_phase_q8 = self.start_sample * 256 - origin
            else:
                # Make source sample zero a valid live steering boundary.
                self.start_sample = (self.start_sample + 31) & ~31
                self._phase_origin = self.start_sample * 256
            if self.start_sample >= 2**53:
                raise ValueError("Scheduled start exceeds the sample-clock range")
            try:
                sequence = next(iter(sequences))
                def arm(target, epochs):
                    self._send(target, Command.TXSTREAM_ARM, payload=_ARM.pack(
                        self.start_sample, sequence, *epochs, self.start_phase_q8))
                if len(plans) == 1:
                    arm(*plans[0])
                else:
                    # Independent controllers arm concurrently. Join every
                    # request before aborting, so a late ARM cannot race CLOSE.
                    by_board = {target[0]: (target, epochs) for target, epochs in plans}
                    self.transmitter._parallel(
                        lambda _, board: arm(*by_board[board]) if board in by_board else None
                    )
                self._wait(lambda s: s["state"] in ("running", "drained", "starved"), lead_time + 1)
            except BaseException as error:
                self._abort(error)
                raise
            self.started = True
            self._auto_start = False
            return self.start_sample

    def finish(self):
        """Mark the exact source end; subsequent output is silence plus static beams."""
        with self._lock:
            if self.closed:
                raise RuntimeError("Stream is closed")
            try:
                if self._auto_start:
                    self.start()
                # Queue after the final DATA on the same transport. Waiting for
                # all sensor snapshots can exhaust the FIFO at high rates.
                for target in self.targets:
                    self._send(target, Command.TXSTREAM_FINISH, self.offset)
                self._wait(lambda s: s["end"] == self.offset, 5)
                self.finished = True
            except BaseException as error:
                self._abort(error)
                raise

    def wait(self, *, timeout=10):
        """Wait until a finished stream has drained, retaining the static scene."""
        with self._lock:
            if not self.started or not self.finished:
                raise RuntimeError("Start and finish the stream before waiting for its end")
            return self._wait(lambda s: s["state"] == "drained", timeout)

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
                    reports = self._reports
                    with reports.changed:
                        before = [reports.stream_clock.latest.get((board, a)) for a in ids]
                    self._send(target, Command.TXSTREAM_CLOSE)

                    def stopped():
                        for a, boot, previous in zip(ids, boots, before):
                            current = reports.stream_clock.latest.get((board, a))
                            if current is None or current is previous:
                                return False
                            if current.boot_id == boot and current.stream_session == self.session:
                                return False
                        return True

                    with reports.changed:
                        if not reports.changed.wait_for(stopped, timeout=5):
                            raise TimeoutError("Sensor did not acknowledge stream closure")
                        for a in ids:
                            item = reports.streams.get((board, a))
                            if item is not None and item[1]["state"] == "closed":
                                self._final_states.append(dict(item[1]))
                except Exception as error:
                    errors.append(error)
                    try:
                        _rpc(board, "set_iq_tx", {"muted": True})
                    except Exception as mute_error:
                        error.add_note(f"Fallback mute also failed: {mute_error}")
            self.closed = True
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
