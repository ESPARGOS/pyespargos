"""IQ transmission, aperture steering and waveform sources, independent of Qt."""

__all__ = ["IQTransmitter"]

import concurrent.futures
from contextlib import contextmanager
import secrets
import struct
import zlib

import numpy as np

from .stream_sources import Stream
from .calibration import Antenna
from .iq_stream_protocol import Uplink
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
        self._stream = None
        self._schedules = set()
        self._stream_scene = {}
        self.signals = TXSignals(self)
        self.workers = concurrent.futures.ThreadPoolExecutor(max_workers=len(geometry.hosts))
        try:
            self.boards = list(owner.boards)
            self.positions = geometry.positions(self.boards) if geometry.indices is not None else None
            self.capture = TXStatus(self.boards)
            self.uplink = Uplink(self.boards)
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

    def _parallel(self, fn, boards=None):
        futures = []
        try:
            for i, board in enumerate(self.boards):
                if boards is None or board in boards:
                    futures.append(self.workers.submit(fn, i, board))
            return [future.result() for future in futures]
        finally:
            # Even on interruption, finish outstanding writes before sending cancellation.
            concurrent.futures.wait(futures)

    def read(self):
        self.states = self._parallel(lambda i, b: _rpc(b, "get_iq_tx"))
        return self.states

    def schedule_steering(self, changes, *, start_sample=None, lead_time=0.5):
        """Schedule beam directions on the playback clock.

        Each change is (sample offset, signal or stream, Beamspace or Weights).
        Weights maps Antenna identities to complex multipliers of the signal
        amplitude. Selected antennas omitted from that entry use unit weight.
        The returned context manager cancels outstanding changes on exit.
        """
        from .iq_steering_schedule import SteeringSchedule

        with self.owner._lock:
            if self.signals._editing:
                raise RuntimeError("Finish editing the waveforms before scheduling steering")
            schedule = SteeringSchedule(self, changes, start_sample, lead_time)
            self._schedules.add(schedule)
            return schedule

    def open_stream(self, *, signal):
        """Open the live source of a prepared signal."""
        with self.owner._lock:
            if signal not in self.signals or not isinstance(signal.waveform, Stream):
                raise ValueError("Select a beam with a Stream source on this transmitter")
            source = signal.waveform
            return self._open_writer(
                signal=signal,
                scene=self._stream_scene,
                mode=source.mode,
                sample_rate_hz=source.sample_rate_hz,
                modulation=source.modulation,
                packet_gap_us=source.packet_gap_us,
                amplitude=signal.amplitude,
                steering=signal.steering,
                antennas=signal.antennas,
            )

    def _open_writer(self, *, signal=None, **settings):
        from .iq_stream import IQStream

        if self.closed or self.signals._editing:
            raise RuntimeError("Finish preparing the transmitter before opening a stream")
        if self._stream is not None and not self._stream.closed:
            raise RuntimeError("Close the current stream before opening another")
        self._stream = IQStream(self, **settings)
        self._stream.signal = signal
        return self._stream

    def open_configured_stream(self):
        """Open the prepared Stream beam using the controllers' saved modulation settings."""
        from .steering import Weights
        from .stream_sources import SingleCarrier, GFSK

        with self.owner._lock:
            profiles = [_rpc(board, "get_iq_tx_stream_config") for board in self.boards]
            self.read()
            first = next((p for p in profiles if p.get("beam", {}).get("antennas")), None)
            if first is None:
                raise ValueError("Prepare a Stream beam before connecting")
            antennas, values, scene = [], {}, {}
            for bi, (board, profile) in enumerate(zip(self.boards, profiles)):
                beam = profile.get("beam", {})
                if not beam.get("antennas"):
                    continue
                if any(profile.get(k) != first.get(k) for k in ("mode", "sample_rate_hz", "table_hex")):
                    raise ValueError("Save matching stream modulation settings on every controller")
                if not beam.get("ready"):
                    raise ValueError(f"{board.host}: waiting for the prepared Stream beam")
                ids = beam["antennas"]
                if len(set(ids)) != len(ids) or any(not isinstance(a, int) or not 0 <= a < 8 for a in ids):
                    raise ValueError("Invalid saved stream antenna selection")
                for key in ("real", "imag", "boot_id", "crc32", "samples"):
                    if len(beam.get(key, [])) != 8:
                        raise ValueError("Incomplete saved stream beam")
                for a in ids:
                    state = self.states[bi]["sensors"][a]
                    if any(state.get(k) != beam[k][a] for k in ("boot_id", "crc32", "samples")):
                        raise ValueError(f"{board.host}: stream beam changed since its settings were read")
                    antenna = Antenna(board.host, a)
                    antennas.append(antenna)
                    scene[antenna] = (beam["boot_id"][a], beam["crc32"][a])
                    values[antenna] = complex(beam["real"][a], beam["imag"][a])
            modulation = {"single_carrier": SingleCarrier, "gfsk": GFSK}[first["mode"]].from_payload(bytes.fromhex(first["table_hex"])) if first["mode"] != "cs8" else None
            stream = self._open_writer(
                scene=scene,
                mode=first["mode"],
                sample_rate_hz=first["sample_rate_hz"],
                modulation=modulation,
                amplitude=1,
                antennas=antennas,
                steering=Weights(values),
                **({"packet_gap_us": first.get("ui", {}).get("gfsk_gap", 20000)} if first["mode"] == "gfsk" else {}),
            )
            return stream

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
        stream_error = None
        if muted and self._stream is not None and not self._stream.closed:
            try:
                self._stream.close()
            except Exception as error:
                stream_error = error
        if not muted:
            self.read()
            self.signals._check_beam_calibration()
        try:

            def apply(i, b):
                if not getattr(self, "authorization", [True] * len(self.boards))[i]:
                    if muted:
                        return
                    raise PermissionError(f"{b.host}: enable transmit authorization in controller Settings")
                settings = _rpc(b, "set_iq_tx", {"muted": muted})
                if self.capture is None:
                    return
                targets = {a: 0 if muted else sensor["amplitude"] for a, sensor in enumerate(settings["sensors"]) if muted or sensor.get("enabled")}

                def applied(state):
                    for a, amplitude in targets.items():
                        sensor = state["sensors"][a]
                        if not sensor.get("enabled"):
                            if muted:
                                continue
                            return False
                        if abs(sensor["applied_amplitude"] - amplitude) > 1e-7:
                            return False
                    return True

                # Unmuting a long waveform can take time to render. Wait for
                # the sensors before starting an upload on the same UART.
                if not applied(settings):
                    self.capture.wait(b, applied, 10)

            self._parallel(apply)
            if stream_error is not None:
                raise stream_error
        except Exception:
            if not muted:
                self._parallel(lambda i, b: _rpc(b, "set_iq_tx", {"muted": True}))
            raise

    def settings(self, payload):
        if self._stream is not None and not self._stream.closed:
            raise RuntimeError("Close the stream before changing TX settings")
        self._parallel(lambda i, b: _rpc(b, "set_iq_tx", payload))
        return self.read()

    def modes(self, modes):
        if self._stream is not None and not self._stream.closed:
            raise RuntimeError("Close the stream before changing sensor modes")

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

        return self.capture.wait(board, done, timeout)

    @contextmanager
    def _edit_replay(self, board, antennas, modes, resize):
        if not resize:
            yield
            return

        # Resizing requires the replay engine to release both sample banks.
        # The applied RX role acknowledges this even when no IQ is delivered.
        try:
            _rpc(board, "set_iq_tx", {"muted": True, "modes": ["iq_rx" if a in antennas else mode for a, mode in enumerate(modes)]})
            self.capture.wait(board, lambda s: all(s["sensors"][a]["applied_mode"] == "iq_rx" and not s["sensors"][a]["enabled"] for a in antennas), 10)
            yield
        finally:
            board.iq.set_config({"modes": modes})
            self.capture.wait(board, lambda s: all(s["sensors"][a]["applied_mode"] == "iq_tx" for a in antennas), 10)

    @property
    def samples(self):
        """Shared replay length in complex samples at the configured transmit rate."""
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
        resize_groups = set()
        for i, groups in enumerate(plans):
            sensors = self.states[i]["sensors"]
            for antennas, _ in groups:
                if any(not sensors[a]["enabled"] or sensors[a]["samples"] != samples for a in antennas):
                    resize_groups.add((i, tuple(antennas)))
        if resize_groups:
            if start_sample is not None:
                raise ValueError("Scheduled updates require running TX with unchanged replay length")
            # Resizing restarts these antennas. Invalidate all affected corrections
            # together before the controller workers start changing modes.
            self.owner.calibrations = self.owner.calibrations.without(Antenna(self.boards[i].host, a) for i, antennas in resize_groups for a in antennas)

        def apply_board(i, board):
            scene = {}

            def remember(state, antennas):
                for a in antennas:
                    sensor = state["sensors"][a]
                    scene[Antenna(board.host, a)] = (sensor["boot_id"], sensor["crc32"])

            scheduled = []
            for antennas, sources in plans[i]:
                modes = self.states[i]["modes"]
                resize = (i, tuple(antennas)) in resize_groups
                with self._edit_replay(board, antennas, modes, resize):
                    records = []
                    file_slot = 0
                    for source, record in sources:
                        recipe = record["waveform"]
                        if isinstance(source, IQSamples):
                            recipe.update(slot=file_slot, crc32=self._upload(i, file_slot, source._words, antennas))
                            file_slot += 1
                        records.append(record)
                    payload = {"beams": records}
                    if start_sample is not None:
                        payload.update(start_sample=start_sample, epoch=self.states[i]["schedule_epoch"])
                    if start_sample is None:
                        state = _waveform(board, payload, 10, wait=self.capture.wait)
                        if any(isinstance(source, Stream) for source, _ in sources):
                            remember(state, antennas)
                    else:
                        # Queue every disjoint sensor group before waiting for
                        # the shared boundary; waiting here would make later groups late.
                        generation = _rpc(board, "set_iq_tx_waveform", payload)["generation"]
                        scheduled.append((generation, antennas, any(isinstance(source, Stream) for source, _ in sources)))
                if resize:
                    self.capture.wait(board, lambda s: all(s["sensors"][a]["enabled"] and s["sensors"][a]["applied_mode"] == "iq_tx" for a in antennas), 10)
                for a in antennas:
                    self.states[i]["sensors"][a].update(samples=samples, enabled=True)
            for generation, antennas, has_stream in scheduled:
                state = self._wait_generation(board, generation, antennas)
                if has_stream:
                    remember(state, antennas)
            return scene

        try:
            scenes = self._parallel(apply_board)
            self._stream_scene = {a: identity for scene in scenes for a, identity in scene.items()}
        except Exception:
            self.mute(True)
            raise

    def calibrate(self, *, method="reciprocity", path="reference"):
        if self._stream is not None and not self._stream.closed:
            raise RuntimeError("Close the stream before calibrating")
        try:
            return self.owner.calibrate(tx_method=method, path=path)
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
        with self.owner._lock:
            if self.closed:
                return
            self.closed = True
            schedules = tuple(self._schedules)
        errors = []
        if self.boards:
            try:
                self.mute(True)
            except Exception as error:
                errors.append(error)
        for schedule in schedules:
            try:
                schedule.close()
            except Exception as error:
                errors.append(error)
        try:
            if self.capture:
                self.capture.close()
        except Exception as error:
            errors.append(error)
        finally:
            self.workers.shutdown(wait=True)
        if errors:
            raise ExceptionGroup("Could not confirm TX shutdown", errors)
