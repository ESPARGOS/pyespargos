"""Sample-clock beam changes for cyclic playback and live IQ transmission."""

from dataclasses import dataclass, replace
import math
import secrets
import struct
import time

from .calibration import CalibrationState
from .iq_stream_protocol import Command
from .iq_tx_control import _rpc, estimated_playback_sample
from .stream_sources import Stream
from .steering import Beamspace, Weights, antenna_weight


_WEIGHT = struct.Struct("<ff")
_LIVE_BEAM = 4
_CAPACITY = 256
_SENSOR_ERRORS = {
    0x101: "insufficient memory",
    0x103: "playback settings changed",
    0x106: "unsupported steering configuration",
    0x107: "missed steering deadline",
    0x10C: "steering queue is full",
}


@dataclass
class _SensorPlan:
    turns: dict
    playback_state: tuple
    stream_session: int
    missing_before: int


def _playback_state(sensor):
    """These fields change if an antenna restarts or its waveform is replaced."""
    return (sensor["boot_id"], sensor["epoch"], sensor["sync_seq"],
            sensor["applied_sample_rate_hz"], sensor["applied_generation"])


def _check_available(board, antenna, sensor):
    if not sensor["enabled"] or not sensor["sync_seq"]:
        raise ValueError(f"{board.host} antenna {antenna}: playback must be synchronized")
    if sensor["steer_free"] != _CAPACITY:
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
        if not isinstance(offset, int) or not 0 <= offset < 2**53:
            raise ValueError("Offsets must be nonnegative sample counts below 2**53")
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
            raise ValueError("Allow at least 0.1 seconds to deliver steering updates")
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
        read_at = time.monotonic()
        states = self._read()
        self._compile(timelines, states)
        position = max(estimated_playback_sample(states[b]["sensors"][a])
                       for b, plans in self._plans.items() for a in plans)
        if start_sample is not None:
            if not isinstance(start_sample, int) or not 0 <= start_sample < 2**53:
                raise ValueError("Start sample must be a nonnegative integer below 2**53")
            if start_sample % 32 and any(plan.stream_session for sensors in self._plans.values()
                                         for plan in sensors.values()):
                raise ValueError("Live steering must start at a multiple of 32 samples")
            if start_sample <= position:
                raise ValueError("Choose a future playback sample")
        else:
            lead = math.ceil(self._sample_rate_hz * (lead_time + time.monotonic() - read_at))
            start_sample = (position + lead + 31) & ~31
        if start_sample + max(max(timeline) for timeline in timelines.values()) >= 2**53:
            raise ValueError("Absolute playback sample exceeds the supported clock range")
        self.start_sample = start_sample
        try:
            self._parallel(self._send_turns)
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
                    plans[a] = _SensorPlan({}, _playback_state(sensor), self._stream.session if live else 0, sensor["steer_missing"])
                    clocks.add((sensor["applied_sample_rate_hz"], sensor["sync_seq"]))
                plan = plans[a]
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
                    plan.turns.setdefault(offset, {})[slot] = weight
            if geometric:
                self.tx.owner.calibrations.require_coherent(calibration)
        if len(clocks) != 1:
            raise ValueError("Schedule requires one common calibrated sample grid")
        self._sample_rate_hz, self._sync_sequence = clocks.pop()

    def _check(self, board, a, state):
        plan = self._plans[board][a]
        if plan.stream_session and (self._stream.closed or self._stream.session != plan.stream_session):
            raise RuntimeError(f"{board.host} antenna {a}: live stream closed or restarted during scheduled steering")
        if not state["enabled"] or _playback_state(state) != plan.playback_state:
            raise RuntimeError(f"{board.host} antenna {a}: playback grid or waveform changed")
        if state.get("steer_id") != self.id:
            return False
        if state["steer_missing"] != plan.missing_before:
            raise RuntimeError(f"{board.host} antenna {a}: missing steering update")
        error = state["steer_error"]
        if error:
            detail = _SENSOR_ERRORS.get(error, f"steering error {error}")
            raise RuntimeError(f"{board.host} antenna {a}: {detail}")
        return True

    def _send_turns(self, _, board):
        # Antennas sharing an instant, beam mask and ordinal use one UART
        # broadcast. Each still gets its own calibrated complex weights.
        packets = {}
        credits = {}
        plans = self._plans[board]
        for a, plan in plans.items():
            _, epoch, _, _, generation = plan.playback_state
            credits[a] = _CAPACITY
            for ordinal, (offset, weights) in enumerate(sorted(plan.turns.items())):
                mask = sum(1 << slot for slot in weights)
                target = struct.pack("<III", epoch, generation, plan.stream_session)
                target += b"".join(_WEIGHT.pack(weights[b].real, weights[b].imag) for b in sorted(weights))
                packets.setdefault((offset, ordinal, mask), {})[a] = target
        for (offset, ordinal, mask), targets in sorted(packets.items()):
            sample = self.start_sample + offset
            if any(ordinal >= credits[a] for a in targets):
                def space(state):
                    for a in targets:
                        sensor = state["sensors"][a]
                        if self._check(board, a, sensor):
                            credits[a] = sensor["steer_received"] + sensor["steer_free"]
                    return all(ordinal < credits[a] for a in targets)
                # Long dwells may leave a full queue for several seconds.
                timeout = max(5, offset / self._sample_rate_hz + 5)
                self.tx.capture.wait(board, space, timeout, antennas=targets)
            payload = struct.pack("<QI", sample, self._sync_sequence)
            payload += b"".join(targets[a] for a in sorted(targets))
            self.tx.uplink.send(board, Command.STEER_AT, sum(1 << a for a in targets),
                                self.id, ordinal, mask, payload)

    def _wait_played(self, _, board, timeout):
        plans = self._plans[board]
        last_samples = {a: self.start_sample + max(plan.turns) for a, plan in plans.items()}

        def played(state):
            complete = True
            for a, plan in plans.items():
                sensor = state["sensors"][a]
                owned = self._check(board, a, sensor)
                if owned and sensor["steer_applied"] == len(plan.turns):
                    continue
                if sensor["sample_position"] > last_samples[a] and (
                        not owned or sensor["steer_received"] < len(plan.turns)):
                    raise RuntimeError(f"{board.host} antenna {a}: final steering update missing at playback")
                complete = False
            return complete
        return self.tx.capture.wait(board, played, timeout, antennas=plans)

    def wait(self, timeout=10):
        """Wait for the last change and update the signals' steering settings."""
        if self.closed:
            raise RuntimeError("Steering schedule is closed")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Timeout must be finite and nonnegative")
        states = self._parallel(lambda i, b: self._wait_played(i, b, timeout))
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
            self.tx.uplink.send(board, Command.STEER_CANCEL, mask, self.id)
            # Only the sensor receipt confirms the cancellation.
            self.tx.capture.wait(board, lambda state: all(
                state["sensors"][a].get("steer_cancelled") == self.id or
                state["sensors"][a].get("boot_id") != antennas[a].playback_state[0]
                for a in antennas), 2, antennas=antennas)
        with self.tx.owner._lock:
            if not self.closed:
                self._parallel(cancel)
                self.closed = True
                self.tx._schedules.discard(self)
