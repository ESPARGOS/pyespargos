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

    Use ``Pool.tx.open_stream()``. IQ8 accepts interleaved signed I8/Q8 bytes,
    an (N, 2) int8 array, or normalized complex samples. BPSK31 accepts bytes
    containing individual 0/1 bits; pulse shaping runs on the sensors.

    Existing cyclic beams remain additive contributions. Their configuration,
    RF settings and calibration must stay fixed until close(). Streaming
    currently requires synchronized 16 MSa/s replay and a prepared waveform.
    """

    RATE = 16_000_000
    CAPACITY = 32768
    CHUNK = 1024

    def __init__(self, transmitter, *, mode="iq8", sample_rate_hz=16000,
                 amplitude=0.1, antennas=None, steering=None):
        self.transmitter = transmitter
        self.closed = False
        self.started = self.finished = False
        self.offset = 0
        self.session = secrets.randbits(32) or 1
        self._lock = threading.RLock()
        self.targets = []
        if mode not in ("iq8", "bpsk31"):
            raise ValueError("mode must be iq8 or bpsk31")
        if mode == "iq8" and sample_rate_hz not in (8000, 16000):
            raise ValueError("IQ8 sample rate must be 8000 or 16000 Sa/s")
        if not math.isfinite(amplitude) or not 0 <= amplitude <= 1:
            raise ValueError("amplitude must lie between 0 and 1")
        if steering is not None and not isinstance(steering, (Beamspace, Weights)):
            raise TypeError("steering must be Beamspace, Weights, or None")
        self.mode = mode
        self.steering = steering
        self.sample_rate_hz = int(sample_rate_hz) if mode == "iq8" else 31.25
        self.stride = 2 if mode == "iq8" else 1
        transmitter.read()
        transmitter.signals._check_beam_calibration()
        self.antennas = transmitter.signals._antennas(antennas)
        if isinstance(steering, Beamspace):
            states = [CalibrationState.from_tx_status(a.host, transmitter.states[bi]["sensors"][a.antid])
                      for bi, b in enumerate(transmitter.boards) for a in self.antennas if a.host == b.host]
            transmitter.owner.calibrations.require_coherent(states)
        try:
            for bi, board in enumerate(transmitter.boards):
                ids = [a.antid for a in self.antennas if a.host == board.host]
                if not ids:
                    continue
                state = transmitter.states[bi]
                if any(state["sensors"][a]["applied_mode"] != "iq_tx" or
                       state["sensors"][a].get("applied_sample_rate_hz", state.get("sample_rate_hz")) != self.RATE or
                       not state["sensors"][a]["samples"] for a in ids):
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
                payload = struct.pack("<II24f", 1 if mode == "iq8" else 2,
                                      int(sample_rate_hz) if mode == "iq8" else 0,
                                      *weights.real, *weights.imag, *delays)
                self._send(target, 1, payload=payload)
            self._wait(lambda states: all(s.get("session") == self.session and s["state"] == "buffering" for s in states), 5, opening=True)
        except BaseException as error:
            self._abort(error)
            raise

    def _send(self, target, command, offset=0, units=0, payload=b""):
        board, ids, _ = target
        header = struct.pack("<IBBBBIII", 0x31535449, 1, command,
                             sum(1 << a for a in ids), 0, self.session, offset, units)
        return board.control.fetch("iq_tx_stream", header + payload)

    def _states(self, *, opening=False, allow_fault=False):
        result = []
        for board, ids, boots in self.targets:
            states = _rpc(board, "get_iq_tx_stream")["sensors"]
            for a, boot in zip(ids, boots):
                s = states[a]
                if not s.get("available") or s.get("boot_id") != boot:
                    raise RuntimeError(f"{board.host}/{a}: sensor restarted or streaming status disappeared")
                if not opening and s.get("session") != self.session:
                    raise RuntimeError(f"{board.host}/{a}: stream session changed")
                if not allow_fault and s.get("session") == self.session and (s["state"] == "fault" or s.get("error")):
                    detail = {0x106: "unsupported scene: use ≤2048 replay samples and 8 kSa/s when mixing",
                              0x107: "replay refill deadline missed", 0x10C: "input FIFO underrun"}.get(s["error"], "stream configuration/state error")
                    raise RuntimeError(f"{board.host}/{a}: {detail} ({s['error']:#x})")
                result.append(s)
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
            time.sleep(.02)

    def status(self):
        """Return status and FIFO counters for the selected sensors."""
        with self._lock:
            return self._states()

    def _encode(self, samples):
        if isinstance(samples, (bytes, bytearray, memoryview)):
            data = bytes(samples)
        elif self.mode == "bpsk31":
            a = np.asarray(samples)
            if a.ndim != 1 or not np.all((a == 0) | (a == 1)):
                raise ValueError("BPSK31 input must contain individual 0/1 bits")
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
                raise ValueError("Use complex samples, an (N, 2) int8 array, or interleaved IQ8 bytes")
        if len(data) % self.stride or (self.mode == "bpsk31" and any(b > 1 for b in data)):
            raise ValueError("Invalid sample/bit encoding")
        return data

    def write(self, samples, *, timeout=10):
        """Append samples/bits with backpressure; return once all sensors ACK.

        Before start(), prefill at least 1024 IQ samples or four BPSK31 bits.
        After start(), keep calling write() before the FIFO runs empty.
        """
        with self._lock:
            if self.closed or self.finished:
                raise RuntimeError("Stream is closed or already finished")
            data = self._encode(samples)
            count = len(data) // self.stride
            begin, end = self.offset, self.offset + count
            if end > 0x7FFFFFFF:
                raise ValueError("Stream exceeds the protocol's sample counter range")
            if not self.started and end > self.CAPACITY:
                raise ValueError("Prefill exceeds FIFO capacity; start before writing more")
            self._timeout(timeout)
            try:
                return self._write_data(data, begin, end, count, timeout)
            except BaseException as error:
                self._abort(error)
                raise

    def _write_data(self, data, begin, end, count, timeout):
        deadline = time.monotonic() + timeout
        sent, retry_at = begin, 0.0
        while True:
            states = self._states()
            accepted = min(s["accepted"] for s in states)
            consumed = min(s["consumed"] for s in states)
            if accepted >= end:
                self.offset = end
                return count
            if time.monotonic() >= deadline:
                raise TimeoutError("Stream write timed out; sensor FIFO did not acknowledge or drain")
            if accepted < sent and time.monotonic() >= retry_at:
                # Repair the whole outstanding window in order. Retrying only
                # one chunk per timeout would stall behind every packet that
                # followed a lost chunk, even though the UART has spare capacity.
                self._send_data(data, begin, max(begin, accepted), sent)
                retry_at = time.monotonic() + .15
            limit = min(end, consumed + self.CAPACITY, sent + 8*self.CHUNK)
            if sent < limit:
                self._send_data(data, begin, sent, limit)
                sent = limit
                retry_at = time.monotonic() + .15
            else:
                time.sleep(.01)

    def _send_data(self, data, begin, offset, end):
        while offset < end:
            units = min(self.CHUNK, end-offset)
            payload = data[(offset-begin)*self.stride:(offset-begin+units)*self.stride]
            for target in self.targets:
                self._send(target, 2, offset, units, payload)
            offset += units

    def start(self, *, lead_time=.5):
        """Arm a common future source origin on the calibrated replay grid.

        Unmute the prepared scene first. The reader joins the streaming ring
        without restarting, preserving the stored scene's calibration.
        """
        with self._lock:
            if self.closed or self.started:
                raise RuntimeError("Stream is closed or already started")
            if not math.isfinite(lead_time) or lead_time < .1:
                raise ValueError("lead_time must be finite and at least 0.1 seconds")
            if self.offset < (1024 if self.mode == "iq8" else 4):
                raise ValueError("Prefill at least 1024 IQ samples or four BPSK31 bits")
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
                states = [CalibrationState.from_tx_status(a.host, self.transmitter.states[bi]["sensors"][a.antid])
                          for bi, b in enumerate(self.transmitter.boards) for a in self.antennas if a.host == b.host]
                self.transmitter.owner.calibrations.require_coherent(states)
            if len(sequences) != 1:
                raise ValueError("Selected sensors do not share a synchronization sequence")
            self.start_sample = int(max(positions)) + math.ceil(lead_time * self.RATE)
            if self.start_sample >= 2**53:
                raise ValueError("Scheduled start exceeds the sample-clock range")
            try:
                for target, epochs in plans:
                    self._send(target, 3, payload=struct.pack("<QI8I", self.start_sample, next(iter(sequences)), *epochs))
                self._wait(lambda states: all(s["state"] in ("running", "drained") for s in states), lead_time)
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
                        time.sleep(.05)
                        sensors = _rpc(board, "get_iq_tx_stream")["sensors"]
                        if all(sensors[a].get("available") and
                               (sensors[a].get("boot_id") != boot or
                                (sensors[a].get("session") == self.session and sensors[a]["state"] == "closed"))
                               for a, boot in zip(ids, boots)):
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
