"""Sample-clock beam changes for cyclic playback and live IQ transmission."""

from dataclasses import dataclass, replace
import math
import secrets
import struct
import time

from . import iq_stream_protocol as protocol
from .calibration import CalibrationState
from .iq_stream_protocol import Command
from .iq_tx_control import _rpc, _wait
from .stream_sources import Stream
from .steering import Beamspace, Weights, antenna_weight


_ENTRY = struct.Struct("<IIff")
_LIVE_BEAM = 4
_CAPACITY = 256
_UPLOAD_CHUNK = 48
_PREPARED = 1
_ARMED = 2
_SENSOR_ERRORS = {
    0x101: "insufficient memory",
    0x103: "playback settings changed",
    0x106: "unsupported steering configuration",
    0x107: "missed steering deadline",
}


@dataclass
class _SensorPlan:
    entries: list
    playback_state: tuple
    stream_session: int


def _playback_state(sensor):
    """These fields change if an antenna restarts or its waveform is replaced."""
    return (sensor["boot_id"], sensor["epoch"], sensor["sync_seq"],
            sensor["applied_sample_rate_hz"], sensor["applied_generation"])


def _check_available(board, antenna, sensor):
    if not sensor["enabled"] or not sensor["sync_seq"]:
        raise ValueError(f"{board.host} antenna {antenna}: playback must be synchronized")
    if sensor.get("steer_id") and not sensor["steer_error"]:
        finished = sensor["steer_state"] == _ARMED and sensor["steer_applied"] == sensor["steer_loaded"]
        if not finished:
            raise RuntimeError(f"{board.host} antenna {antenna}: another steering schedule is active")


def _changes_by_signal(changes, signals, live):
    timelines = {}
    for offset, signal, steering in changes:
        if signal is None or (signal is not live and signal not in signals):
            raise ValueError("Select a signal from this transmitter")
        if signal is not live and isinstance(signal.waveform, Stream):
            if getattr(live, "signal", None) is not signal:
                raise ValueError("Open and start the stream beam before scheduling it")
            signal = live
        if not isinstance(offset, int) or not 0 <= offset <= 0xffffffff:
            raise ValueError("Offsets must be unsigned 32-bit sample counts")
        if not isinstance(steering, (Beamspace, Weights)):
            raise ValueError("Use Beamspace or Weights for each steering change")
        timeline = timelines.setdefault(signal, {})
        if offset in timeline:
            raise ValueError("A beam can only change once at a given sample")
        timeline[offset] = steering
    if not timelines:
        raise ValueError("A steering schedule needs at least one change")
    return timelines


class SteeringSchedule:
    """A finite sequence of (sample offset, signal or stream, steering) changes."""

    def __init__(self, transmitter, changes, start_sample=None, lead_time=0.5):
        if not math.isfinite(lead_time) or lead_time < 0.1:
            raise ValueError("Allow at least 0.1 seconds to arm the schedule")
        if transmitter.closed:
            raise RuntimeError("Transmitter is closed")
        self.tx = transmitter
        self.id = secrets.randbelow(0xffffffff) + 1
        self.closed = False
        self._stream = transmitter._stream
        if self._stream is not None and self._stream.closed:
            self._stream = None
        timelines = _changes_by_signal(changes, transmitter.signals, self._stream)
        self._final = {signal: timeline[max(timeline)] for signal, timeline in timelines.items()}
        hosts = {a.host for signal in timelines for a in signal.antennas}
        self._plans = {board: {} for board in transmitter.boards if board.host in hosts}
        self._compile(timelines, self._read())
        if start_sample is not None:
            if not isinstance(start_sample, int) or not 0 <= start_sample < 2**53:
                raise ValueError("Start sample must be a nonnegative integer below 2**53")
            if start_sample % 32 and any(plan.stream_session for sensors in self._plans.values()
                                         for plan in sensors.values()):
                raise ValueError("Live steering must start at a multiple of 32 samples")
        try:
            self._parallel(self._upload)
            self._wait("steer_loaded", 5)
            self._parallel(self._prepare)
            self._wait("steer_state", 30, expected=_PREPARED)
            self._arm(start_sample, lead_time)
            self._wait("steer_state", 2, expected=_ARMED)
        except BaseException as error:
            try:
                self.close()
            except Exception as cleanup_error:
                error.add_note(f"Could not cancel schedule on all antennas: {cleanup_error}")
            raise

    def _parallel(self, fn):
        results = self.tx._parallel(fn, boards=self._plans)
        return dict(zip(self._plans, results))

    def _read(self):
        return self._parallel(lambda _, board: _rpc(board, "get_iq_tx"))

    def _compile(self, timelines, states):
        boards = {board.host: (i, board) for i, board in enumerate(self.tx.boards)}
        live_antennas = set(self._stream.antennas) if self._stream else set()
        clocks = set()
        for signal, timeline in timelines.items():
            streamed = signal is self._stream
            amplitude = signal._amplitude if streamed else signal.amplitude
            waveform = None if streamed else signal.waveform
            geometric = any(isinstance(value, Beamspace) for value in timeline.values())
            calibration = []
            for antenna in signal.antennas:
                i, board = boards[antenna.host]
                a = antenna.antid
                sensor = states[board]["sensors"][a]
                plans = self._plans[board]
                if a not in plans:
                    _check_available(board, a, sensor)
                    live = antenna in live_antennas
                    if live and not self._stream.started:
                        raise ValueError("Start the stream before scheduling its antennas")
                    plans[a] = _SensorPlan([], _playback_state(sensor), self._stream.session if live else 0)
                    clocks.add((sensor["applied_sample_rate_hz"], sensor["sync_seq"]))
                plan = plans[a]
                if len(plan.entries) + len(timeline) > _CAPACITY:
                    raise ValueError("Each sensor accepts up to 256 steering changes")
                if plan.stream_session and any(offset % 32 for offset in timeline):
                    raise ValueError("Live steering offsets must be multiples of 32 playback samples")
                if geometric:
                    calibration.append(CalibrationState.from_tx_status(board.host, sensor))
                position = self.tx.positions[i, a] if geometric and self.tx.positions is not None else None
                slot = _LIVE_BEAM if streamed else self.tx.signals._assigned_to(antenna, loops_only=True).index(signal)
                for offset, steering in timeline.items():
                    weight = amplitude * antenna_weight(steering, antenna, position, sensor["frequency_hz"], waveform)
                    if not math.isfinite(abs(weight)) or abs(weight) > 1.000001:
                        raise ValueError("Steering exceeds the sensor weight range")
                    plan.entries.append((offset, slot, weight.real, weight.imag))
            if geometric:
                self.tx.owner.calibrations.require_coherent(calibration)
        if len(clocks) != 1:
            raise ValueError("Schedule requires one common calibrated sample grid")
        self._sample_rate_hz, self._sync_sequence = clocks.pop()
        for sensors in self._plans.values():
            for plan in sensors.values():
                plan.entries.sort()

    def _upload(self, _, board):
        sensors = self._plans[board]
        longest = max(len(plan.entries) for plan in sensors.values())
        # Interleave antennas to leave each sensor time to receive its UART data.
        for offset in range(0, longest, _UPLOAD_CHUNK):
            for a, plan in sensors.items():
                chunk = plan.entries[offset:offset + _UPLOAD_CHUNK]
                if chunk:
                    payload = b"".join(_ENTRY.pack(*row) for row in chunk)
                    protocol.send(board, Command.STEER_LOAD, 1 << a, self.id, offset, len(chunk), payload)
                    time.sleep(0.006)

    def _groups(self, board):
        """Antennas with the same entry count and stream can share a command."""
        masks = {}
        for a, plan in self._plans[board].items():
            key = len(plan.entries), plan.stream_session
            masks[key] = masks.get(key, 0) | (1 << a)
        return masks.items()

    def _prepare(self, _, board):
        for (count, session), mask in self._groups(board):
            protocol.send(board, Command.STEER_PREPARE, mask, self.id, count=count, payload=struct.pack("<I", session))

    def _arm(self, start_sample, lead_time):
        read_at = time.monotonic()
        states = self._read()
        positions = []
        for board, plans in self._plans.items():
            for a, plan in plans.items():
                sensor = states[board]["sensors"][a]
                if not sensor["enabled"] or _playback_state(sensor) != plan.playback_state:
                    raise RuntimeError(f"{board.host} antenna {a}: playback grid or scene changed")
                positions.append(sensor["sample_position"])
        if start_sample is None:
            lead = math.ceil(self._sample_rate_hz * (lead_time + time.monotonic() - read_at))
            start_sample = (max(positions) + lead + 31) & ~31
        elif start_sample < max(positions) + self._sample_rate_hz // 20:
            raise ValueError("Choose a later start sample. Sensors need at least 50 ms after preparation.")
        self.start_sample = start_sample

        def arm(_, board):
            epochs = [s["epoch"] for s in states[board]["sensors"]]
            for (count, session), mask in self._groups(board):
                payload = struct.pack("<QI8II", start_sample, self._sync_sequence, *epochs, session)
                protocol.send(board, Command.STEER_ARM, mask, self.id, count=count, payload=payload)

        self._parallel(arm)

    def _wait(self, field, timeout, expected=None):
        def wait_board(_, board):
            def ready(state):
                if self.closed:
                    raise RuntimeError("Steering schedule is closed")
                done = True
                for a, plan in self._plans[board].items():
                    sensor = state["sensors"][a]
                    if sensor.get("steer_id") != self.id:
                        done = False
                        continue
                    if not sensor["enabled"] or _playback_state(sensor) != plan.playback_state:
                        raise RuntimeError(f"{board.host} antenna {a}: playback grid or scene changed")
                    code = sensor["steer_error"]
                    if code:
                        detail = _SENSOR_ERRORS.get(code, f"steering error {code}")
                        applied = sensor["steer_applied"]
                        raise RuntimeError(f"{board.host} antenna {a}: {detail} ({applied}/{len(plan.entries)} changes applied)")
                    target = len(plan.entries) if expected is None else expected
                    if sensor[field] != target:
                        done = False
                return done

            return self.tx.capture.wait(board, ready, timeout, antennas=self._plans[board])

        return self._parallel(wait_board)

    def wait(self, timeout=10):
        """Wait for the last change and update the signals' steering settings."""
        if self.closed:
            raise RuntimeError("Steering schedule is closed")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Timeout must be finite and nonnegative")
        states = self._wait("steer_applied", timeout)
        with self.tx.owner._lock:
            if self.closed:
                raise RuntimeError("Steering schedule is closed")
            for signal, steering in self._final.items():
                if signal is self._stream:
                    signal.steering = steering
                    signal = getattr(signal, "signal", None)
                    if signal is None:
                        continue
                signal._settings = replace(signal._settings, steering=steering)
        return states

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as error:
            if exc is None:
                raise
            exc.add_note(f"Could not release steering schedule: {error}")

    def close(self):
        """Cancel pending changes and wait until the sensors release the schedule."""
        def cancel(_, board):
            antennas = self._plans[board]
            mask = sum(1 << a for a in antennas)
            protocol.send(board, Command.STEER_CANCEL, mask, self.id)
            # The HTTP response confirms forwarding. Sensors acknowledge separately.
            _wait(board, lambda state: all(state["sensors"][a].get("steer_id") != self.id
                                           for a in antennas), 2)
        with self.tx.owner._lock:
            if not self.closed:
                self._parallel(cancel)
                self.closed = True
                self.tx._schedules.discard(self)
