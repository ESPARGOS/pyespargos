"""Shared TX command acknowledgement helpers."""

import json
import struct
import threading
import time
import zlib
from .board import EspargosHTTPStatusError


class _SensorTXError(RuntimeError):
    def __init__(self, board, antenna, code):
        self.code = code
        super().__init__(f"{board.host}, sensor {antenna}: waveform error {code}")


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
        self.status = {b: [None] * 8 for b in boards}
        self.subscriptions = []
        try:
            for b in boards:
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
            if tag == 6 and size in (100, 108):
                v = struct.unpack_from("<9I3f" + str((size - 48) // 4) + "I", data, offset)
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
                state.update(applied_mode=("wifi", "iq_rx", "iq_tx")[v[13]], epoch=v[15], origin_ns=(v[25] | v[26] << 32) if size == 108 else 0)
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
