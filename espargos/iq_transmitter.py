"""IQ transmission, aperture steering and waveform sources, independent of Qt."""

__all__ = ["IQTransmitter"]

import concurrent.futures
from contextlib import contextmanager
import secrets
import struct
import threading
import zlib

import numpy as np

from .iq_tx_control import TXStatus, _SensorTXError, _rpc, _waveform
from .waveforms import IQSamples
from .tx_signal import TXSignals


class IQTransmitter:
    """Serialized TX operations; independent boards run in parallel.

    Obtained through Pool.tx, this capability borrows the owner's connections
    and IQ processor. Closing it mutes TX without closing those connections.
    """

    def __init__(self, owner):
        self.closed = False
        self.owner = owner
        self.geometry = geometry = owner._geometry
        self.boards = []
        self.receiver = self.capture = None
        self.uploaded = {}
        self.signals = TXSignals(self)
        self.workers = concurrent.futures.ThreadPoolExecutor(max_workers=len(geometry.hosts))
        try:
            self.boards = list(owner.boards)
            self.positions = geometry.positions(self.boards) if geometry.indices is not None else None
            self.capture = TXStatus(self.boards)
            self.receiver = owner.iq
            self.authorization = self.authorized()
            self._parallel(lambda i, b: _rpc(b, "set_iq_tx", {"muted": True}) if self.authorization[i] else None)
            self.states = self.read()
        except Exception as error:
            try:
                self.close()
            except Exception as cleanup_error:
                error.add_note(str(cleanup_error))
            raise

    def _parallel(self, fn):
        futures = [self.workers.submit(fn, i, b) for i, b in enumerate(self.boards)]
        results, errors = [], []
        for f in futures:
            try:
                results.append(f.result())
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]
        return results

    def read(self):
        self.states = self._parallel(lambda i, b: _rpc(b, "get_iq_tx"))
        return self.states

    def authorized(self):
        return self._parallel(lambda i, b: _rpc(b, "get_transmit_authorization")["authorized"])

    def reconnect(self):
        """Recover lost streams and device state, always leaving TX muted."""
        self.authorization = self.authorized()
        self.mute(True)
        with self.capture.changed:
            self.capture.status = {b: [None] * 8 for b in self.boards}

        self.owner.reconnect()
        self.uploaded.clear()
        return self.read()

    def mute(self, muted):
        try:

            def apply(i, b):
                if not getattr(self, "authorization", [True] * len(self.boards))[i]:
                    if muted:
                        return
                    raise PermissionError(f"{b.host}: enable transmit authorization in controller Settings")
                _rpc(b, "set_iq_tx", {"muted": muted})
                return self.capture.wait(b, lambda s: all(not x["enabled"] or abs(x["applied_amplitude"]) < 1e-8 for x in s["sensors"]), 10) if muted and self.capture is not None else None

            self._parallel(apply)
        except Exception:
            if not muted:
                self._parallel(lambda i, b: _rpc(b, "set_iq_tx", {"muted": True}))
            raise

    def settings(self, payload):
        self._parallel(lambda i, b: _rpc(b, "set_iq_tx", payload))
        return self.read()

    def modes(self, modes):
        def apply(i, b):
            _rpc(b, "set_iq_tx", {"muted": True, "modes": modes[i]})
            self.capture.wait(b, lambda s: all(x["applied_mode"] == modes[i][a] for a, x in enumerate(s["sensors"])), 10)

        self._parallel(apply)
        return self.read()

    def _upload(self, board_index, slot, words, antennas):
        samples = len(words)
        crc = zlib.crc32(words.tobytes())
        board = self.boards[board_index]
        boots = tuple(self.states[board_index]["sensors"][a]["boot_id"] for a in antennas)
        signatures = {(board_index, a, slot): (crc, samples, boot) for a, boot in zip(antennas, boots)}
        if all(self.uploaded.get(key) == value for key, value in signatures.items()):
            return crc
        for attempt in range(2):
            generation = secrets.randbits(32) or 1
            try:
                for offset in range(0, samples, 1000):
                    part = words[offset : offset + 1000]
                    header = struct.pack("<IBBBB7I", 0x31585449, 3, 7, sum(1 << a for a in antennas), slot, generation, offset, samples, crc, len(part), 0xFFFFFFFF, 0xFFFFFFFF)
                    board.control.fetch("upload_iq_tx_waveform", header.ljust(68, b"\0") + part.tobytes())
                self._wait_generation(board, generation, antennas)
                break
            except (ConnectionError, TimeoutError, _SensorTXError) as error:
                # Restart an interrupted transfer once. A missing UART chunk
                # reports INVALID_STATE; live replay uses its own copy.
                if attempt or isinstance(error, _SensorTXError) and error.code != 0x103:
                    raise
        self.uploaded.update(signatures)
        return crc

    def _wait_generation(self, board, generation, antennas, timeout=10):
        def done(state):
            for a in antennas:
                sensor = state["sensors"][a]
                if sensor["generation"] == generation and sensor["error"]:
                    raise _SensorTXError(board, a, sensor["error"])
            return all(state["sensors"][a]["generation"] == generation for a in antennas)

        self.capture.wait(board, done, timeout)

    @contextmanager
    def _edit_replay(self, board, antennas, modes, resize):
        if not resize:
            yield
            return
        # RX mode status can arrive while the engine is still restarting.
        # A fresh chunk from every target proves it can accept a new buffer.
        trigger = board.iq.get_config()["trigger"]
        subscription = None
        try:
            _rpc(board, "set_iq_tx", {"muted": True})
            board.iq.set_config({"modes": ["iq_rx" if a in antennas else m for a, m in enumerate(modes)], "trigger": {"mode": "interval", "interval_chunks": 8192, "burst_chunks": 1}})
            generation = board.iq.get_config()["config_generation"]
            seen, ready = set(), threading.Event()

            def received(message):
                if message.payload.config_generation == generation and message.antenna_id in antennas:
                    seen.add(message.antenna_id)
                    if len(seen) == len(antennas):
                        ready.set()

            subscription = board.iq.subscribe_chunks(received)
            if not ready.wait(10):
                raise TimeoutError(f"{board.host}: receivers did not become ready for replay resize")
            self.capture.wait(board, lambda s: all(s["sensors"][a]["applied_mode"] == "iq_rx" and not s["sensors"][a]["enabled"] for a in antennas), 10)
            yield
        finally:
            if subscription:
                board.iq.unsubscribe(subscription)
            board.iq.set_config({"modes": modes, "trigger": trigger})
            self.capture.wait(board, lambda s: all(s["sensors"][a]["applied_mode"] == "iq_tx" for a in antennas), 10)

    @property
    def samples(self):
        """Shared replay length in complex samples (80 MSa/s)."""
        return self.signals._samples

    @samples.setter
    def samples(self, value):
        with self.update(samples=value):
            pass

    def update(self, *, samples=None, start_sample=None):
        """Batch signal edits, optionally scheduling them on a replay boundary.

        Without a boundary, edits apply as soon as each sensor is ready. A
        scheduled update requires synchronized running TX and an unchanged
        replay length. Failed hardware updates mute transmission.
        """
        return self.signals.update(samples=samples, start_sample=start_sample)

    def _apply_signals(self, plans, start_sample):
        samples = self.samples
        if start_sample is not None:
            self.read()
            for i, groups in enumerate(plans):
                for antennas, _ in groups:
                    if any(not self.states[i]["sensors"][a]["enabled"] or self.states[i]["sensors"][a]["samples"] != samples for a in antennas):
                        raise ValueError("Scheduled updates require running TX with unchanged replay length")

        def apply_board(i, board):
            scheduled = []
            for antennas, sources in plans[i]:
                modes = self.states[i]["modes"]
                resize = any(self.states[i]["sensors"][a]["samples"] != samples or not self.states[i]["sensors"][a]["enabled"] for a in antennas)
                with self._edit_replay(board, antennas, modes, resize):
                    records = []
                    for slot, (source, record) in enumerate(sources):
                        recipe = record["waveform"]
                        if isinstance(source, IQSamples):
                            recipe.update(slot=slot, crc32=self._upload(i, slot, source._words, antennas))
                        records.append(record)
                    payload = {"beams": records}
                    if start_sample is not None:
                        payload.update(start_sample=start_sample, epoch=self.states[i]["schedule_epoch"])
                    if start_sample is None:
                        _waveform(board, payload, 10, wait=self.capture.wait)
                    else:
                        # Queue every disjoint sensor group before waiting for
                        # the shared boundary; waiting here would make later groups late.
                        generation = _rpc(board, "set_iq_tx_waveform", payload)["generation"]
                        scheduled.append((generation, antennas))
                if resize:
                    self.capture.wait(board, lambda s: all(s["sensors"][a]["enabled"] and s["sensors"][a]["applied_mode"] == "iq_tx" for a in antennas), 10)
                for a in antennas:
                    self.states[i]["sensors"][a].update(samples=samples, enabled=True)
            for generation, antennas in scheduled:
                self._wait_generation(board, generation, antennas)

        try:
            self._parallel(apply_board)
        except Exception:
            self.mute(True)
            raise

    def calibrate(self, *, method="reciprocity", path="reference"):
        try:
            return self.receiver.calibrate_tx(method=method, path=path, cable_lengths=self.geometry.cables, cable_velocity_factors=self.geometry.velocity_factors)
        finally:
            self.read()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as cleanup_error:
            if exc is None:
                raise
            exc.add_note(f"TX shutdown also failed: {cleanup_error}")

    def close(self):
        if self.closed:
            return
        self.closed = True
        errors = []
        if self.boards:
            try:
                self.mute(True)
            except Exception as error:
                errors.append(str(error))
        if self.capture:
            self.capture.close()
        self.workers.shutdown(wait=True)
        if errors:
            raise RuntimeError("Could not confirm shutdown: " + "; ".join(errors))
