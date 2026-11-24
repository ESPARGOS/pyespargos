"""Shared TX command acknowledgement helpers."""

import json
import threading
import time
from . import iq_stream_protocol, sensor_status
from .board import EspargosHTTPStatusError


class _SensorTXError(RuntimeError):
    def __init__(self, board, antenna, code):
        self.code = code
        super().__init__(f"{board.host}, sensor {antenna}: waveform error {code}")


def estimated_playback_sample(sensor):
    """Project the last sensor clock reading to the time of the controller reply."""
    age_ms = sensor["sample_age_ms"]
    if age_ms > 2000:
        raise RuntimeError("Sensor playback clock is stale")
    return sensor["sample_position"] + age_ms * sensor["applied_sample_rate_hz"] // 1000


def _rpc(board, path, payload=None):
    if payload is None:
        return board.control.get_json(path)
    try:
        result = board.control.post_json(path, payload)
    except EspargosHTTPStatusError as error:
        # These errors occur while serializing status, after set_iq_tx has
        # applied the settings. Read status again without repeating the write.
        if path == "set_iq_tx" and error.status == 503 and error.body in ("not enough memory for JSON response", "not enough memory for TX status"):
            return board.control.get_json("get_iq_tx")
        raise
    return None if result == "ok" else json.loads(result)


def _wait(board, predicate, timeout):
    deadline = time.monotonic() + timeout
    while True:
        state = _rpc(board, "get_iq_tx")
        if predicate(state):
            return state
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{board.host}: sensors did not acknowledge TX settings")
        time.sleep(0.03)


def _waveform(board, recipe, timeout, wait=_wait):
    started = time.monotonic()
    retry = recipe.get("start_sample") is None
    antennas = recipe["beams"][0]["waveform"]["antennas"] if "beams" in recipe else recipe["antennas"]
    for attempt in range(2):
        try:
            generation = _rpc(board, "set_iq_tx_waveform", recipe)["generation"]

            def applied(state):
                for a in antennas:
                    sensor = state["sensors"][a]
                    if sensor["generation"] == generation and sensor["error"]:
                        raise _SensorTXError(board, a, sensor["error"])
                return all(state["sensors"][a]["generation"] == generation for a in antennas)

            remaining = max(0, timeout - (time.monotonic() - started))
            return wait(board, applied, min(remaining, 1.0) if retry and not attempt else remaining)
        except (ConnectionError, TimeoutError, EspargosHTTPStatusError) as error:
            if attempt or not retry or isinstance(error, EspargosHTTPStatusError) and error.status != 503:
                raise
            # Repeating an immediate recipe is safe even if its HTTP response
            # was lost. Scheduled commands and sensor errors are not retried.
            if not isinstance(error, TimeoutError):
                time.sleep(0.1)


class TXStatus:
    """Wait for streamed TX acknowledgements, with bounded HTTP recovery."""

    def __init__(self, boards):
        self.changed = threading.Condition()
        self.clock = sensor_status.ReportClock()
        self.stream_clock = sensor_status.ReportClock()
        self.status = {b: [None] * 8 for b in boards}
        self.streams = {}
        self.queues = {}
        self.subscriptions = []
        report_types = (sensor_status.MAGIC, iq_stream_protocol.TXSTREAM_STATUS)
        try:
            for board in boards:
                subscription = board.subscribe_sensor_messages(
                    report_types, lambda message, b=board: self.receive_status(b, message))
                self.subscriptions.append((board, subscription))
        except Exception:
            self.close()
            raise

    def receive_status(self, board, message):
        if int.from_bytes(message.payload[:4], "little") == iq_stream_protocol.TXSTREAM_STATUS:
            report = iq_stream_protocol.decode_status(message)
            if report is not None:
                key = (board, report.antenna)
                with self.changed:
                    if self.stream_clock.accept(key, report):
                        self.streams[key] = (time.monotonic(), report.state)
                        self.changed.notify_all()
            return
        report = sensor_status.decode(message)
        if report is None:
            return
        a = report.antenna
        key = (board, a)
        with self.changed:
            last_report = self.clock.latest.get(key)
            if not self.clock.accept(key, report):
                return
            rebooted = last_report is not None and last_report.boot_id != report.boot_id
            previous = self.status[board][a]
            if rebooted:
                self.queues.pop(key, None)
                self.status[board][a] = previous = None
            mode = sensor_status.mode_name(report.mode) if report.mode is not None else None
            if mode is not None and mode != "iq_tx":
                self.queues.pop(key, None)

            previous_stream = self.stream_clock.latest.get(key)
            stream_rebooted = previous_stream is not None and previous_stream.boot_id != report.boot_id
            if (report.stream_session is not None or stream_rebooted) and self.stream_clock.accept(key, report):
                if (previous_stream is None or previous_stream.boot_id != report.boot_id
                        or previous_stream.stream_session != report.stream_session):
                    self.streams.pop(key, None)
                    if report.stream_session == 0:
                        self.streams[key] = (time.monotonic(), dict(
                            session=0, state="closed", error=0, antid=a, boot_id=report.boot_id, available=True))

            playback = sensor_status.playback_state(report)
            queue = sensor_status.steering_state(report)
            if queue and mode in (None, "iq_tx"):
                cached = self.queues.get(key)
                self.queues[key] = (report.boot_id, {**(cached[1] if cached else {}), **queue})
            mode_changed = mode is not None and (previous or {}).get("applied_mode") != mode
            if playback or queue or mode_changed:
                state = dict(previous or {}, boot_id=report.boot_id, report_sequence=report.sequence)
                if mode_changed:
                    for field in tuple(state):
                        if field.startswith("steer_"):
                            del state[field]
                    state["applied_mode"] = mode
                    if mode != "iq_tx":
                        state.update(enabled=False, sync_seq=0)
                        for field in ("generation", "error", "samples"):
                            state.setdefault(field, 0)
                state.update(playback)
                if key in self.queues:
                    state.update(self.queues[key][1])
                self.status[board][a] = state
            self.changed.notify_all()

    def wait(self, board, predicate, timeout, *, antennas=()):
        """Prefer streamed acknowledgements, recover dropped UDP status via HTTP.

        For a long wait, pass the antennas being watched. As long as each of
        them keeps reporting, the stream is sufficient to track progress.

        Never hold the collector lock during a control request: stream reception
        must remain able to make progress while that request is in flight.
        """
        deadline = time.monotonic() + timeout
        next_poll = min(deadline, time.monotonic() + 0.25)
        previous = [None] * 8
        fallback = [None] * 8
        while True:
            with self.changed:
                live = self.status[board]
                sensors = [s if s is not None else old for s, old in zip(live, fallback)]
                complete = all(s is not None for s in sensors)
                if complete:
                    state = {"sensors": sensors}
                    try:
                        if predicate(state):
                            return state
                    except KeyError:
                        # A newly attached client may have only a position or queue update.
                        complete = False
                now = time.monotonic()
                left = next_poll - now
                if left > 0:
                    # Each sensor wakes us separately. Keep collecting streamed
                    # acknowledgements instead of fetching a full JSON status
                    # after every partial update (which strains controller RAM).
                    self.changed.wait(left)
                    continue
                # Check outside the lock over HTTP only when a watched sensor
                # goes quiet. Always make one final read at the timeout.
                fresh = antennas and all(live[a] is not previous[a] for a in antennas)
                previous = list(live)
                if complete and fresh and now < deadline:
                    next_poll = min(deadline, now + 0.25)
                    continue
            # A missing stream packet does not mean the sensor rejected a command.
            state = _rpc(board, "get_iq_tx")
            fallback = state["sensors"]
            if predicate(state):
                return state
            if time.monotonic() >= deadline:
                details = ", ".join(f"{a}: mode={s.get('applied_mode')}, generation={s.get('generation')}, error={s.get('error')}" for a, s in enumerate(state["sensors"]))
                raise TimeoutError(f"{board.host}: sensor settings were not acknowledged ({details})")
            next_poll = min(deadline, time.monotonic() + 0.25)

    def close(self):
        errors = []
        while self.subscriptions:
            board, subscription = self.subscriptions.pop()
            try:
                board.unsubscribe_sensor_messages(subscription)
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Could not unsubscribe sensor messages", errors)
