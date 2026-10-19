"""Blocking TX sessions, aperture steering and waveform sources, independent of Qt."""

__all__ = ["TXLayout", "TXSession", "TXMonitor", "default_beam", "steering", "waveform", "load_iq"]

import concurrent.futures
import threading
from contextlib import contextmanager
import math
import secrets
import struct
import zlib
from pathlib import Path

import numpy as np
import yaml

import espargos
from espargos.combined_array import parse_combined_array_config
from espargos.iq_tx_calibration import _Captures, _rpc, _waveform


def default_beam(boards):
    return dict(
        type="cw",
        amplitude=0.1,
        offset_mhz=1.0,
        start_mhz=-40.0,
        stop_mhz=40.0,
        duration_us=0.4,
        delay_us=0.0,
        file="",
        mode="beamspace",
        u=0.0,
        v=0.0,
        squint=False,
        phase=[[0.0] * 8 for _ in range(boards)],
        delay=[[0.0] * 8 for _ in range(boards)],
        taper=[[1.0] * 8 for _ in range(boards)],
    )


class TXLayout:
    def __init__(self, hosts=None, config=None, spacing=0.06):
        if not np.isfinite(spacing) or spacing <= 0:
            raise ValueError("Aperture spacing must be positive and finite")
        self.spacing = spacing
        self.cables = self.velocity_factors = None
        if config:
            data = config if isinstance(config, dict) else yaml.safe_load(Path(config).read_text())["combined-array"]
            self.indices, names, self.cables, self.velocity_factors, _, _, _ = parse_combined_array_config(data)
            self.hosts = list(names.values())
            self.names = list(names)
            if hosts:
                if len(hosts) != len(self.hosts):
                    raise ValueError("Host overrides must match the number of boards in the layout")
                self.hosts = hosts
        else:
            self.hosts = hosts or []
            if len(self.hosts) != 1:
                raise ValueError("Use --config to specify the physical layout of multiple arrays")
            self.names = self.hosts
            self.indices = np.arange(8).reshape(2, 4)

    def positions(self, boards):
        """Outward-looking x/y coordinates, indexed by board and antenna ID."""
        rows, cols = self.indices.shape
        result = np.zeros((len(boards), 8, 2))
        for row, col in np.ndindex(self.indices.shape):
            board, local = divmod(int(self.indices[row, col]), 8)
            for a in range(8):
                if boards[board].revision.antenna_id_to_row_col(a) == divmod(local, 4):
                    result[board, a] = ((cols - 1) / 2 - col, (rows - 1) / 2 - row)
        return result * self.spacing


def steering(positions, u, v, frequency, squint=False):
    norm = max(1, np.hypot(u, v))
    seconds = (positions[..., 0] * u + positions[..., 1] * v) / (norm * 299792458)
    return -360 * frequency * seconds, seconds * 80e6 if squint else np.zeros_like(seconds)


def load_iq(path, samples):
    """Read complex .npy or two-column I/Q text, normalize to signed 10-bit."""
    data = np.load(path, allow_pickle=False) if Path(path).suffix.lower() == ".npy" else np.loadtxt(path, delimiter="," if Path(path).suffix.lower() == ".csv" else None)
    if data.ndim == 2 and data.shape[1] == 2:
        data = data[:, 0] + 1j * data[:, 1]
    if data.ndim != 1 or not np.iscomplexobj(data) or len(data) != samples or not np.isfinite(data).all():
        raise ValueError(f"Expected {samples} finite complex samples or {samples} rows of I,Q")
    peak = max(abs(data.real).max(), abs(data.imag).max())
    if peak == 0:
        return np.zeros(samples, dtype="<u4")
    i = np.rint(data.real * 511 / peak).astype(np.int32)
    q = np.rint(data.imag * 511 / peak).astype(np.int32)
    return ((i & 1023) | ((q & 1023) << 10)).astype("<u4")


def waveform(beam, samples):
    recipe = {"type": beam["type"], "samples": samples, "amplitude": beam["amplitude"]}
    if beam["type"] in ("cw", "two-tone"):
        if abs(beam["offset_mhz"]) > 40 or beam["type"] == "two-tone" and abs(beam["offset_mhz"]) >= 20:
            raise ValueError("CW offset must be within ±40 MHz; two-tone half-span must be inside ±20 MHz")
        recipe["frequency_hz"] = beam["offset_mhz"] * 1e6
    elif beam["type"] == "lfm":
        active, offset = round(beam["duration_us"] * 80), round(beam["delay_us"] * 80)
        if abs(active - beam["duration_us"] * 80) > 1e-6 or abs(offset - beam["delay_us"] * 80) > 1e-6:
            raise ValueError("Chirp timing must use whole samples (0.0125 µs)")
        if active < 1 or active + offset > samples or offset < 0:
            raise ValueError("Chirp duration and delay must fit inside the replay buffer")
        recipe.update(start_hz=beam["start_mhz"] * 1e6, stop_hz=beam["stop_mhz"] * 1e6, chirp_samples=active, pause_samples=samples - active, offset_samples=offset)
    elif beam["type"] in ("noise", "qpsk"):
        recipe["seed"] = 0x657370
    return recipe


class TXSession:
    """Serialized TX operations; independent boards run in parallel.

    An optional running IQPool shares the demo's existing connections. Closing
    an attached session mutes TX but leaves that pool and its boards open.
    """

    def __init__(self, layout, pool=None):
        self.closed = False
        self.owns_pool = pool is None
        self.layout = layout
        self.boards = []
        self.pool = self.capture = self.monitor = None
        self.uploaded = {}
        self.file_sources = {}
        self.workers = concurrent.futures.ThreadPoolExecutor(max_workers=len(layout.hosts))
        try:
            if pool is None:
                for host in layout.hosts:
                    self.boards.append(espargos.Board(host))
            else:
                if [b.host for b in pool.boards] != layout.hosts:
                    raise ValueError("Pool board order must match the TX layout")
                self.boards = list(pool.boards)
            self.positions = layout.positions(self.boards)
            self.capture = _Captures(self.boards)
            self.pool = pool if pool is not None else espargos.IQPool(self.boards)
            if self.owns_pool:
                # TX needs reliable status/calibration bursts, not continuous
                # high-rate RX. Avoid losing a whole calibration to one UDP
                # datagram while the GUI is drawing.
                self.parallel(lambda i, b: b.start(transports=["websocket"]))
                self.pool.start()
            self.authorization = self.authorized()
            self.parallel(lambda i, b: _rpc(b, "set_iq_tx", {"muted": True}) if self.authorization[i] else None)
            self.states = self.read()
        except Exception as error:
            try:
                self.close()
            except Exception as cleanup_error:
                error.add_note(str(cleanup_error))
            raise

    def parallel(self, fn):
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
        self.states = self.parallel(lambda i, b: _rpc(b, "get_iq_tx"))
        return self.states

    def authorized(self):
        return self.parallel(lambda i, b: _rpc(b, "get_transmit_authorization")["authorized"])

    def reconnect(self):
        """Recover lost streams and device state, always leaving TX muted."""
        self.authorization = self.authorized()
        self.mute(True)
        with self.capture.changed:
            self.capture.status = {b: [None] * 8 for b in self.boards}

        def restart(i, board):
            # A rebooted controller has lost the UDP subscription even when
            # the old receive socket has not timed out on the host yet.
            board.stop()
            board.start(transports=["websocket"] if self.owns_pool else None)

        self.parallel(restart)
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
                return self.capture.wait(b, lambda s: all(not x["enabled"] or abs(x["applied_amplitude"]) < 1e-8 for x in s["sensors"]), 10) if muted else None

            self.parallel(apply)
        except Exception:
            if not muted:
                self.parallel(lambda i, b: _rpc(b, "set_iq_tx", {"muted": True}))
            raise

    def settings(self, payload):
        self.parallel(lambda i, b: _rpc(b, "set_iq_tx", payload))
        return self.read()

    def modes(self, modes):
        def apply(i, b):
            _rpc(b, "set_iq_tx", {"muted": True, "modes": modes[i]})
            self.capture.wait(b, lambda s: all(x["applied_mode"] == modes[i][a] for a, x in enumerate(s["sensors"])), 10)

        self.parallel(apply)
        return self.read()

    def upload(self, board_index, slot, path, samples, antennas):
        stat = Path(path).stat()
        source_key = (path, samples, stat.st_mtime_ns, stat.st_size)
        cached = self.file_sources.get((board_index, slot))
        if cached is None or cached[0] != source_key:
            cached = (source_key, load_iq(path, samples))
            self.file_sources[board_index, slot] = cached
        words = cached[1]
        crc = zlib.crc32(words.tobytes())
        board = self.boards[board_index]
        boots = tuple(self.states[board_index]["sensors"][a]["boot_id"] for a in antennas)
        key = board_index, slot
        signature = crc, samples, tuple(antennas), boots
        if self.uploaded.get(key) == signature:
            return crc
        generation = secrets.randbits(32) or 1
        for offset in range(0, samples, 1000):
            part = words[offset : offset + 1000]
            header = struct.pack("<IBBBB7I", 0x31585449, 3, 7, sum(1 << a for a in antennas), slot, generation, offset, samples, crc, len(part), 0xFFFFFFFF, 0xFFFFFFFF)
            board.control.fetch("upload_iq_tx_waveform", header.ljust(68, b"\0") + part.tobytes())
        self.wait_generation(board, generation, antennas)
        self.uploaded[key] = signature
        return crc

    def wait_generation(self, board, generation, antennas, timeout=10):
        def done(state):
            for a in antennas:
                sensor = state["sensors"][a]
                if sensor["generation"] == generation and sensor["error"]:
                    raise RuntimeError(f'{board.host} antenna {a}: waveform error {sensor["error"]}')
            return all(state["sensors"][a]["generation"] == generation for a in antennas)

        self.capture.wait(board, done, timeout)

    @contextmanager
    def edit_replay(self, board, antennas, modes, resize):
        if not resize:
            yield
            return
        # RX mode status can arrive while the engine is still restarting.
        # A fresh chunk from every target proves it can accept a new buffer.
        trigger = board.iq.get_config()["trigger"]
        subscription = None
        try:
            _rpc(board, "set_iq_tx", {"muted": True})
            board.iq.set_config(
                {"modes": ["iq_rx" if a in antennas else m for a, m in enumerate(modes)], "trigger": {"mode": "interval", "interval_chunks": 8192, "burst_chunks": 1}}
            )
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

    def apply(self, beams, samples, frequency):
        # Source recipes remain identical while dragging. Sensors reuse their
        # cached source and delayed waveform; only the complex weights change.
        def apply_board(i, board):
            antennas = [a for a, mode in enumerate(self.states[i]["modes"]) if mode == "iq_tx"]
            if not antennas:
                return
            modes = self.states[i]["modes"]
            resize = any(self.states[i]["sensors"][a]["samples"] not in (0, samples) for a in antennas)
            with self.edit_replay(board, antennas, modes, resize):
                records = []
                for slot, beam in enumerate(beams):
                    recipe = waveform(beam, samples)
                    if beam["type"] == "file":
                        recipe.update(slot=slot, crc32=self.upload(i, slot, beam["file"], samples, antennas))
                    recipe["antennas"] = antennas
                    if beam["mode"] == "beamspace":
                        phase, delay = steering(self.positions[i], beam["u"], beam["v"], frequency, beam["squint"])
                    else:
                        phase, delay = np.array(beam["phase"][i]), np.array(beam["delay"][i])
                    weights = np.exp(1j * np.deg2rad(phase)) * np.array(beam["taper"][i]) / len(beams)
                    records.append({"waveform": recipe, "real": weights.real.tolist(), "imag": weights.imag.tolist(), "delay_samples": delay.tolist()})
                _waveform(board, {"beams": records}, 10, wait=self.capture.wait, retry=resize)
            for a in antennas:
                self.states[i]["sensors"][a]["samples"] = samples

        try:
            self.parallel(apply_board)
        except Exception:
            self.mute(True)
            raise

    def configure_monitor(self, config):
        """Enable/update the reference monitor on the detected master controller."""
        board = self.pool.master_board
        monitor = self.monitor or TXMonitor(board)
        try:
            _rpc(board, "set_iq_monitor", {**config, "enabled": True})
        except Exception:
            if self.monitor is None:
                monitor.close()
            raise
        self.monitor = monitor
        return monitor

    def calibrate(self, route="reference"):
        try:
            return self.pool.calibrate_tx(pilot_output=route, cable_lengths=self.layout.cables, cable_velocity_factors=self.layout.velocity_factors)
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
            for stop in (lambda: self.mute(True), lambda: self.parallel(lambda i, b: _rpc(b, "set_iq_monitor", {"enabled": False}))):
                try:
                    stop()
                except Exception as error:
                    errors.append(str(error))
        if self.monitor:
            self.monitor.close()
        if self.capture:
            self.capture.close()
        if self.owns_pool:
            if self.pool:
                self.pool.close()
            for b in self.boards:
                b.close()
        self.workers.shutdown(wait=True)
        if errors:
            raise RuntimeError("Could not confirm shutdown: " + "; ".join(errors))


class TXMonitor:
    def __init__(self, board):
        self.board, self.host = board, board.host
        self.latest, self.error = None, None
        self.key, self.parts = None, {}
        self.subscription = board.subscribe_controller_messages(0x314D5149, self.receive)

    def receive(self, data):
        if len(data) < 64:
            return
        h = np.frombuffer(data[:64], dtype="<u4")
        total, offset, count = map(int, h[7:10])
        if not (256 <= total <= 8188 and count == min(256, total - offset) and offset % 256 == 0 and 0 < count and offset + count <= total and len(data) == 64 + 4 * count):
            return
        key = tuple(h[[1, 2, 12, 13]])
        if self.key != key:
            self.key, self.parts = key, {}
        w = np.frombuffer(data[64:], dtype="<u4").astype(np.int32)
        i, q = w & 1023, (w >> 10) & 1023
        # Controller ADC Q has opposite polarity to conventional complex IQ.
        self.parts[offset] = np.where(i >= 512, i - 1024, i) - 1j * np.where(q >= 512, q - 1024, q)
        if len(self.parts) == math.ceil(total / 256):
            self.latest = np.concatenate([self.parts[k] for k in sorted(self.parts)])
            self.parts = {}

    def close(self):
        self.board.unsubscribe_controller_messages(self.subscription)
