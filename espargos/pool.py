"""A collection of ESPARGOS boards, shared connections and antenna selections."""

from __future__ import annotations

from pathlib import Path
from threading import RLock, Lock, Event, Thread, current_thread
import time
import math
import numpy as np
import yaml

from .array_geometry import ArrayGeometry
from .board import Board
from . import board as board_module
from .calibration import Antenna, Calibration
from .wifi_transceiver import WiFiTransceiver
from .iq_receiver import IQReceiver, DECIM_TO_FS
from .iq_transmitter import IQTransmitter
from .iq_tx_control import _rpc, _wait

__all__ = ["Pool", "AntennaSelection"]


class AntennaSelection:
    """A view of antennas owned by a Pool, with no connections of its own.

    ``antennas`` in select() means hardware IDs (0–7) on each selected board,
    or explicit Antenna identities. Nested selections can only narrow a view.
    """

    def __init__(self, pool, antennas):
        self._pool = pool
        self.antennas = tuple(antennas)

    def select(self, *, boards=None, antennas=None):
        owner = self._pool
        names = dict(zip(owner.names, owner.hosts))
        if isinstance(boards, (str, board_module.Board)):
            boards = [boards]
        hosts = set(owner.hosts)
        if boards is not None:
            hosts = set()
            for name in boards:
                if isinstance(name, board_module.Board):
                    name = name.host
                host = names.get(name, name)
                if host not in owner.hosts:
                    raise ValueError(f"Unknown board {name!r}")
                hosts.add(host)
        requested = None if antennas is None else tuple(antennas)
        if requested is not None:
            if all(isinstance(a, Antenna) for a in requested):
                if any(a not in self.antennas or a.host not in hosts for a in requested):
                    raise ValueError("Requested antennas are outside this selection")
                match = lambda a: a in requested
            else:
                if any(type(a) is not int or not 0 <= a < 8 for a in requested):
                    raise ValueError("Use hardware antenna IDs 0–7 or Antenna identities")
                match = lambda a: a.antid in requested
        else:
            match = lambda a: True
        selected = tuple(a for a in self.antennas if a.host in hosts and match(a))
        if not selected:
            raise ValueError("Selection contains no antennas")
        return AntennaSelection(owner, selected)

    def configure(self, *, mode=None, frequency_hz=None, sample_rate_hz=None, rx_gain=None, gain_mode=None, rf_gain=None, bandwidth_mhz=None, output=None, channel=None, secondary_channel=None):
        """Update only selected antennas; mode changes do not calibrate or start TX.

        RX sample rate and RX output routing are board-wide hardware controls:
        select all eight antennas on an affected board to change those fields.
        WiFi tuning uses channel/secondary_channel (or a channel-center frequency).
        WiFi gain controls remain available through board.wifi_rx.
        """
        owner = self._pool
        if mode not in (None, "wifi", "iq_rx", "iq_tx"):
            raise ValueError("mode must be wifi, iq_rx or iq_tx")
        if frequency_hz is not None and (not math.isfinite(frequency_hz) or frequency_hz <= 0):
            raise ValueError("frequency_hz must be positive and finite")
        rates = {v: k for k, v in DECIM_TO_FS.items()}
        if sample_rate_hz is not None and sample_rate_hz not in rates:
            raise ValueError(f"sample_rate_hz must be one of {tuple(rates)}")
        routes = {"isolation": 0, "reference": 1, "antenna_r": 2, "antenna_l": 3}
        if output is not None and output not in routes:
            raise ValueError(f"output must be one of {tuple(routes)}")
        with owner._lock:
            owner.open()
            plans = []
            for board in owner.boards:
                ids = [a.antid for a in self.antennas if a.host == board.host]
                if not ids:
                    continue
                config = board.iq.get_config()
                modes = list(config["modes"])
                before = modes.copy()
                if mode is not None:
                    for a in ids:
                        modes[a] = mode
                rx = [a for a in ids if modes[a] == "iq_rx"]
                tx = [a for a in ids if modes[a] == "iq_tx"]
                wifi = [a for a in ids if modes[a] == "wifi"]
                if (rx_gain is not None or gain_mode is not None) and (tx or wifi):
                    raise ValueError("rx_gain/gain_mode require an IQ RX selection")
                if (rf_gain is not None or bandwidth_mhz is not None) and (rx or wifi):
                    raise ValueError("rf_gain/bandwidth_mhz require an IQ TX selection")
                if (channel is not None or secondary_channel is not None) and (rx or tx):
                    raise ValueError("channel settings require a WiFi selection")
                if sample_rate_hz is not None and (tx or wifi):
                    raise ValueError("sample_rate_hz currently configures IQ RX only")
                if (sample_rate_hz is not None or output is not None and (rx or wifi)) and len(ids) != 8:
                    raise ValueError("RX sample rate and routing are board-wide; select all eight antennas")
                iq = {"modes": modes} if mode is not None else {}
                receiver = {}
                if frequency_hz is not None:
                    receiver["rf_freq_hz"] = frequency_hz
                if rx_gain is not None:
                    receiver.update(rx_gain=rx_gain, gain_mode=gain_mode or "manual")
                elif gain_mode is not None:
                    receiver["gain_mode"] = gain_mode
                if receiver and rx:
                    iq["receivers"] = [dict(antid=a, **receiver) for a in rx]
                if sample_rate_hz is not None:
                    iq["adc_decimation"] = rates[sample_rate_hz]
                if output is not None and rx:
                    iq["rf_switch"] = routes[output]
                transmitters = []
                for a in tx:
                    settings = {k: v for k, v in dict(frequency_hz=frequency_hz, rf_gain=rf_gain, bandwidth_mhz=bandwidth_mhz, output=output).items() if v is not None}
                    # A previously prepared waveform must not become audible
                    # merely because an antenna changes into TX mode.
                    if before[a] != "iq_tx":
                        settings["amplitude"] = 0
                    if settings:
                        transmitters.append(dict(antid=a, **settings))
                channels = None
                if wifi and any(v is not None for v in (frequency_hz, channel, secondary_channel)):
                    primary = channel
                    if frequency_hz is not None:
                        frequencies = {i: (2484e6 if i == 14 else (2407 + 5 * i) * 1e6) for i in range(1, 15)}
                        found = next((i for i, f in frequencies.items() if f == frequency_hz), None)
                        if found is None or primary is not None and primary != found:
                            raise ValueError("WiFi frequency must match the primary channel center")
                        primary = found
                    if primary is not None and (type(primary) is not int or not 1 <= primary <= 14):
                        raise ValueError("WiFi channel must be 1–14")
                    if secondary_channel not in (None, -1, 0, 1):
                        raise ValueError("secondary_channel must be -1, 0 or 1")
                    channels = board.wifi_rx.get_channel_overrides()
                    if not channels["override_active"]:
                        base = board.wifi_rx.get_config()
                        channels = {"override_active": True, "channel-primary": [base["channel-primary"]] * 8, "channel-secondary": [base["channel-secondary"]] * 8}
                    for a in wifi:
                        if primary is not None:
                            channels["channel-primary"][a] = primary
                        if secondary_channel is not None:
                            channels["channel-secondary"][a] = 2 if secondary_channel == -1 else secondary_channel
                plans.append((board, iq, transmitters, channels, bool(wifi)))
            # All scope checks precede writes, including across multiple boards.
            try:
                for board, iq, transmitters, channels, wifi in plans:
                    if transmitters:
                        _rpc(board, "set_iq_tx", {"transmitters": transmitters})
                    if iq:
                        board.iq.set_config(iq)
                    if mode is not None:
                        _wait(board, lambda s, modes=iq["modes"]: all(x["applied_mode"] == modes[a] for a, x in enumerate(s["sensors"])), 10)
                    if channels:
                        board.wifi_rx.set_channel_overrides(channels)
                    if wifi and output is not None:
                        board.wifi_rx.set_rf_switch(routes[output])
            except Exception:
                for board, *_ in plans:
                    try:
                        _rpc(board, "set_iq_tx", {"muted": True})
                    except Exception:
                        pass
                raise
            finally:
                if owner._wifi is not None and any(mode is not None or p[3] for p in plans):
                    owner._wifi.invalidate_calibration(antennas=self.antennas)
                if mode is not None:
                    owner._refresh_receivers()
                if owner._tx is not None and not owner._tx.closed:
                    owner._tx.read()
        return self

    def start_transmission(self, *, amplitude=1.0):
        """Unmute selected IQ transmitters, scaling their prepared waveforms.

        Starting a subset of a globally muted board keeps other transmitters
        silent. This changes samples, not replay timing or calibration.
        """
        if not math.isfinite(amplitude) or amplitude < 0:
            raise ValueError("amplitude must be nonnegative and finite")
        owner = self._pool
        with owner._lock:
            owner.open()
            plans = []
            for board in owner.boards:
                ids = {a.antid for a in self.antennas if a.host == board.host}
                if not ids:
                    continue
                state = _rpc(board, "get_iq_tx")
                if any(state["sensors"][a]["applied_mode"] != "iq_tx" or not state["sensors"][a]["samples"] for a in ids):
                    raise ValueError("Prepare a waveform and select IQ TX before starting transmission")
                if not _rpc(board, "get_transmit_authorization")["authorized"]:
                    raise PermissionError(f"{board.host}: transmission is not authorized")
                changes = [{"antid": a, "amplitude": amplitude if a in ids else 0} for a in range(8) if a in ids or state["muted"]]
                plans.append((board, changes))
            try:
                for board, changes in plans:
                    _rpc(board, "set_iq_tx", {"transmitters": changes, "muted": False})
                    _wait(board, lambda s, changes=changes: all(abs(s["sensors"][v["antid"]]["applied_amplitude"] - v["amplitude"]) < 1e-6 for v in changes if s["sensors"][v["antid"]]["applied_mode"] == "iq_tx"), 10)
            except Exception:
                for board, _ in plans:
                    try:
                        _rpc(board, "set_iq_tx", {"muted": True})
                    except Exception:
                        pass
                raise

    def stop_transmission(self):
        """Replace selected TX samples with silence without stopping replay."""
        owner = self._pool
        with owner._lock:
            owner.open()
            for board in owner.boards:
                changes = [{"antid": a.antid, "amplitude": 0} for a in self.antennas if a.host == board.host]
                if changes:
                    _rpc(board, "set_iq_tx", {"transmitters": changes})
                    _wait(board, lambda s, changes=changes: all(not s["sensors"][v["antid"]]["enabled"] or abs(s["sensors"][v["antid"]]["applied_amplitude"]) < 1e-6 for v in changes), 10)

    def calibrate(self, *, tx_method="reciprocity", path="reference", duration=2, per_board=True):
        """Calibrate configured roles and return only the requested coverage.

        IQ measurement uses the entire connected reference domain as helpers,
        restores its roles, and currently requires a common IQ frequency/rate.
        WiFi groups retain their individual channels. Set per_board=False only
        for boards connected to a common reference. Geometry does not select
        a reference domain. Stored corrections do not
        become valid again merely by restoring a previous configuration.
        """
        owner = self._pool
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration must be positive and finite")
        if tx_method not in ("monitor", "reciprocity") or path not in ("reference", "antenna_l", "antenna_r"):
            raise ValueError("Unknown TX calibration method or signal path")
        if tx_method == "monitor" and path != "reference":
            raise ValueError("Monitor calibration requires the reference path")
        with owner._lock:
            owner.open()
            configs = [b.iq.get_config() for b in owner.boards]
            owner._refresh_receivers(configs)
            selected = set(self.antennas)
            roles = {Antenna(b.host, a): c["modes"][a] for b, c in zip(owner.boards, configs) for a in range(8)}
            iq_selected = {a for a in selected if roles[a] != "wifi"}
            wifi_selected = selected - iq_selected
            result = Calibration()
            if iq_selected:
                tx_states = [_rpc(b, "get_iq_tx") for b in owner.boards]
                centers, rates = set(), set()
                for b, c, t in zip(owner.boards, configs, tx_states):
                    for a, role in enumerate(c["modes"]):
                        if role == "iq_rx":
                            centers.add(c["receivers"][a]["rf_freq_hz"])
                            rates.add(c["adc_decimation"])
                        elif role == "iq_tx":
                            centers.add(t["sensors"][a]["frequency_hz"])
                if len(centers) != 1 or len(rates) > 1:
                    raise ValueError("IQ calibration currently requires one frequency and RX sample rate across the connected reference domain")
                cables = dict(cable_lengths=owner._geometry.cables, cable_velocity_factors=owner._geometry.velocity_factors)
                if "iq_tx" in roles.values():
                    measured = owner.iq.calibrate_tx(method=tx_method, path=path, **cables)
                else:
                    from .iq_tx_calibration import snapshot

                    try:
                        owner.iq.sync(center_hz=next(iter(centers)), **cables)
                    finally:
                        for b, c in zip(owner.boards, configs):
                            b.iq.set_config({"modes": c["modes"], "receivers": c["receivers"]})
                        for b, c in zip(owner.boards, configs):
                            _wait(b, lambda state, modes=c["modes"]: all(sensor["applied_mode"] == mode for sensor, mode in zip(state["sensors"], modes)), 10)
                    measured = snapshot(owner.iq)
                result = result.merge(measured.select(antennas=iq_selected))
                if owner._wifi is not None:
                    owner._wifi.invalidate_calibration()
            if wifi_selected:
                result = result.merge(
                    owner.wifi.calibrate_channels(antennas=wifi_selected, per_board=per_board, duration=duration, run_in_thread=not owner.wifi.processing, cable_lengths=owner._geometry.cables, cable_velocity_factors=owner._geometry.velocity_factors)
                )
            owner.calibrations = owner.calibrations.merge(result)
            if owner._tx is not None and not owner._tx.closed:
                owner._tx.read()
            return result


class Pool(AntennaSelection):
    """An ESPARGOS system with one owner for all board connections.

    Construction and from_config() perform no hardware I/O. Use as a context
    manager or call open()/close(). Selection views never close the owner.
    ``iq`` exposes IQ reception and ``wifi`` combines WiFi reception and transmission; ``tx`` exposes the existing
    waveform/beam controls over those same boards. Creating a TX controller
    leaves transmission muted. Operations are blocking.
    """

    def __init__(self, boards):
        """Use Board objects or addresses, without requiring antenna geometry.

        Supplied Board objects are reused. This pool owns their lifetime once
        passed in, so do not share them between independently managed pools.
        """
        items = [boards] if isinstance(boards, (str, board_module.Board)) else list(boards)
        if any(not isinstance(b, (str, board_module.Board)) for b in items):
            raise TypeError("Pool expects Board objects or controller addresses")
        hosts = [b if isinstance(b, str) else b.host for b in items]
        self._initialize(ArrayGeometry(hosts))
        self._provided = {b.host: b for b in items if not isinstance(b, str)}

    def _initialize(self, geometry):
        self._geometry = geometry
        self.hosts, self.names = list(geometry.hosts), list(geometry.names)
        self.boards = []
        self._provided = {}
        self._opened = False
        self._iq = self._wifi = self._tx = None
        self._monitor = None
        self._closed = False
        self._lock = RLock()
        self._run_lock = Lock()
        self._processing_stop = Event()
        self._processing_stop.set()
        self._processing_thread = None
        self.calibrations = Calibration()
        super().__init__(self, [Antenna(h, a) for h in self.hosts for a in range(8)])

    @classmethod
    def from_config(cls, config, *, hosts=None):
        """Read the existing combined-array YAML format, or an equivalent dict.

        Optional host overrides follow the named board order in the file.
        Demo-specific YAML sections are ignored. No modes or TX are enabled.
        """
        data = yaml.safe_load(Path(config).read_text()) if isinstance(config, (str, Path)) else config
        data = data.get("combined-array", data)
        geometry = ArrayGeometry(hosts, config=data)
        pool = cls.__new__(cls)
        pool._initialize(geometry)
        return pool

    @property
    def indexing_matrix(self):
        return self._geometry.indices

    @property
    def antenna_orientations(self):
        return self._geometry.orientations

    @property
    def reference_cables(self):
        return dict(cable_lengths=self._geometry.cables, cable_velocity_factors=self._geometry.velocity_factors)

    def open(self, *, transports=None):
        with self._lock:
            if self._closed:
                raise RuntimeError("Pool is closed; create a new Pool to reconnect")
            if self._opened:
                return self
            try:
                for host in self.hosts:
                    board = self._provided.get(host)
                    if board is None:
                        board = Board(host)
                    self.boards.append(board)
                    board.start(transports=transports)
                self._transports = transports
                self._opened = True
            except Exception as error:
                try:
                    self.close()
                except Exception as cleanup:
                    error.add_note(str(cleanup))
                raise
            return self

    def reconnect(self):
        """Restart shared streams after a device reboot or connection failure."""
        with self._lock:
            self.open()
            for board in self.boards:
                board.stop()
                board.start(transports=self._transports)

    def reboot(self):
        """Reboot every board. Call reconnect() once the devices are reachable."""
        with self._lock:
            self.open()
            for board in self.boards:
                board.reboot()

    def _refresh_receivers(self, configs=None):
        """Snapshot operating roles outside the callback thread."""
        if self._iq is None and self._wifi is None:
            return
        if configs is None:
            configs = [b.iq.get_config() if b.api_version[0] >= 4 else {"modes": ["wifi"] * 8} for b in self.boards]
        for receiver, mode in ((self._iq, "iq_rx"), (self._wifi, "wifi")):
            if receiver is None:
                continue
            expected = np.zeros((len(self.boards), 2, 4), dtype=bool)
            for i, (board, config) in enumerate(zip(self.boards, configs)):
                for antenna, role in enumerate(config["modes"]):
                    expected[i, *board.revision.antenna_id_to_row_col(antenna)] = role == mode
            receiver._expected = expected

    @property
    def iq(self):
        with self._lock:
            self.open()
            if self._iq is None:
                self._iq = IQReceiver(self)
                self._refresh_receivers()
            return self._iq

    @property
    def wifi(self):
        with self._lock:
            self.open()
            if self._wifi is None:
                self._wifi = WiFiTransceiver(self)
                self._refresh_receivers()
            return self._wifi

    @property
    def tx(self):
        with self._lock:
            self.open()
            if self._tx is None or self._tx.closed:
                self._tx = IQTransmitter(self)
            return self._tx

    def configure_monitor(self, config):
        """Configure the reference monitor on the master controller."""
        from .reference_monitor import ReferenceMonitor

        with self._lock:
            self.open()
            board = self.iq.master_board
            if not config.get("enabled", True):
                _rpc(board, "set_iq_monitor", {"enabled": False})
                if self._monitor is not None:
                    self._monitor.close()
                    self._monitor = None
                return None
            monitor = self._monitor or ReferenceMonitor(board)
            try:
                _rpc(board, "set_iq_monitor", {**config, "enabled": True})
            except Exception:
                if self._monitor is None:
                    monitor.close()
                raise
            self._monitor = monitor
            return monitor

    @property
    def processing(self):
        """Whether the shared background processing worker is running."""
        return not self._processing_stop.is_set()

    def start_processing(self):
        """Process CSI and IQ callbacks in one background thread.

        Receivers created later participate automatically. Callbacks should
        return promptly, since a slow callback delays both receive paths.
        """
        with self._lock:
            self.open()
            if self.processing:
                return
            if self._processing_thread is not None and self._processing_thread.is_alive():
                raise RuntimeError("Processing is still stopping")
            if self._run_lock.locked():
                raise RuntimeError("Pool.run() is already active")
            self._processing_stop.clear()
            self._processing_thread = Thread(target=self._processing_loop, name="espargos-processing", daemon=True)
            self._processing_thread.start()

    def stop_processing(self):
        """Stop callback processing without closing board connections."""
        with self._lock:
            self._processing_stop.set()
            thread = self._processing_thread
        if thread is not None and thread is not current_thread():
            thread.join()

    def _processing_loop(self):
        try:
            while not self._processing_stop.is_set():
                if not self._process():
                    self._processing_stop.wait(0.005)
        finally:
            self._processing_stop.set()

    def _process(self):
        count = 0
        for receiver in (self._wifi, self._iq):
            if self._closed:
                break
            if receiver is not None:
                count += receiver._run(timeout=0)
        return count

    def run(self, timeout=0.005):
        """Process queued CSI and IQ once, for programs without a worker.

        Do not call this while background processing is active. A timeout
        bounds the wait for data, not the time spent executing callbacks.
        """
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        with self._lock:
            self.open()
            if self.processing:
                raise RuntimeError("Stop background processing before calling Pool.run()")
            if not self._run_lock.acquire(blocking=False):
                raise RuntimeError("Pool.run() must not be called concurrently or reentrantly")
        try:
            deadline = time.monotonic() + timeout
            while True:
                count = self._process()
                remaining = deadline - time.monotonic()
                if count or remaining <= 0 or self._closed:
                    return count
                time.sleep(min(0.005, remaining))
        finally:
            self._run_lock.release()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.stop_processing()
        errors = []

        def attempt(fn):
            try:
                fn()
            except Exception as e:
                errors.append(e)

        if self._monitor is not None:
            attempt(lambda: _rpc(self._monitor.board, "set_iq_monitor", {"enabled": False}))
            attempt(self._monitor.close)
            self._monitor = None
        if self._tx is not None and not self._tx.closed:
            attempt(self._tx.close)
        else:
            for b in self.boards:
                if b.api_version[0] >= 4:
                    attempt(lambda b=b: _rpc(b, "set_iq_tx", {"muted": True}))
        for b in self.boards:
            attempt(lambda b=b: b.wifi_tx.set_config({"active_by_antid": [False] * 8}))
            if b.api_version[0] >= 4:
                attempt(b.iq.set_reference)
        for pool in (self._iq, self._wifi):
            if pool is not None:
                attempt(pool._close)
        for b in self.boards:
            attempt(b.close)
        for b in self._provided.values():
            if b not in self.boards:
                attempt(b.close)
        if errors:
            raise ExceptionGroup("Pool shutdown failed", errors)

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            exc.add_note(str(cleanup))
