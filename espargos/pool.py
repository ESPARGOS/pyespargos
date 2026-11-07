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
        from .configuration import configure
        return configure(self, mode=mode, frequency_hz=frequency_hz, sample_rate_hz=sample_rate_hz,
                         rx_gain=rx_gain, gain_mode=gain_mode, rf_gain=rf_gain,
                         bandwidth_mhz=bandwidth_mhz, output=output, channel=channel,
                         secondary_channel=secondary_channel)

    def calibrate_tx_rx_delay(self, *, path="reference", reference_delays_s=None, timeout=5.0):
        """Measure each IQ transmitter's delay relative to calibrated receivers.

        Temporarily receive calibration probes at 80 MSa/s, then restore the RX
        rate and application waveforms without changing the TX replay length.
        Below 80 MSa/s, select all active RX sensors on each affected board.
        Reference-path delays come from board traces and configured cable lengths.
        For antenna paths, supply {(tx_antenna, rx_antenna): delay_seconds}.
        At least one calibrated IQ receiver and transmitter must be running.
        """
        from ._iq_calibration.delay import measure

        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        owner = self._pool
        with owner._lock:
            owner.open()
            return measure(owner, self.antennas, path, reference_delays_s, timeout)

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
            if owner._tx is not None and not owner._tx.closed:
                owner._tx.read()
                owner._tx.signals._check_beam_calibration(self.antennas)
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
                    owner._iq_tx_boards.add(board)
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

        IQ measurement uses only selected sensors and restores their roles.
        Selected IQ sensors need one frequency/rate. Capture routing and triggers
        remain board-wide. With other IQ sensors on the same board, subgroup
        calibration needs an existing synchronized grid at 80 MSa/s.
        Existing calibration groups must be selected in full for recalibration.
        WiFi groups retain their individual channels. Calibrating the whole pool
        on one WiFi channel also updates the CSI backlog corrections and
        timestamp association. Set per_board=False only
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
            configs = [b.iq.get_config() if b.api_version[0] >= 4 else {"modes": ["wifi"] * 8} for b in owner.boards]
            owner._refresh_receivers(configs)
            selected = set(self.antennas)
            roles = {Antenna(b.host, a): c["modes"][a] for b, c in zip(owner.boards, configs) for a in range(8)}
            if any(roles[a] == "ble" for a in selected):
                raise ValueError("Use pool.ble.calibrate() for narrowband BLE phase calibration")
            iq_selected = {a for a in selected if roles[a] != "wifi"}
            wifi_selected = selected - iq_selected
            # Validate every role before either IQ or WiFi acquisition starts.
            tx_states = [_rpc(b, "get_iq_tx") for b in owner.boards] if iq_selected else []
            keys = []
            for bi, (board, config) in enumerate(zip(owner.boards, configs)):
                for a in range(8):
                    antenna = Antenna(board.host, a)
                    if antenna in iq_selected:
                        direction = "tx" if roles[antenna] == "iq_tx" else "rx"
                        frequency = tx_states[bi]["sensors"][a]["frequency_hz"] if direction == "tx" else config["receivers"][a]["rf_freq_hz"]
                        keys.append((antenna, direction, frequency))
            if wifi_selected:
                from .csi_scoped_calibration import states

                current = states(owner.wifi)
                keys.extend((a, "rx", current[a].frequency_hz) for a in wifi_selected)
            owner.calibrations._check_replacement(keys)
            result = Calibration()
            if iq_selected:
                from ._iq_calibration.scope import CalibrationScope
                from ._iq_calibration.orchestration import calibrate, snapshot

                scope = CalibrationScope(owner.iq, iq_selected)
                scope.check_sync(configs)
                tx_selected = [a for a in iq_selected if roles[a] == "iq_tx"]
                if tx_selected and tx_method == "reciprocity" and len(tx_selected) < 2:
                    raise ValueError("Reciprocity calibration requires at least two selected IQ transmitters")
                centers, rates = set(), set()
                for bi, a in scope.pairs:
                    if configs[bi]["modes"][a] == "iq_rx":
                        centers.add(configs[bi]["receivers"][a]["rf_freq_hz"])
                        rates.add(configs[bi]["adc_decimation"])
                    else:
                        centers.add(tx_states[bi]["sensors"][a]["frequency_hz"])
                if len(centers) != 1 or len(rates) > 1:
                    raise ValueError("Selected IQ sensors require one frequency and RX sample rate")
                cables = dict(cable_lengths=owner._geometry.cables, cable_velocity_factors=owner._geometry.velocity_factors)
                try:
                    if tx_selected:
                        measured = calibrate(owner.iq, method=tx_method, path=path, scope=scope, **cables)
                    else:
                        owner.iq._sync(center_hz=next(iter(centers)), scope=scope, publish=False, **cables)
                        measured = snapshot(owner.iq, scope=scope)
                except Exception:
                    owner.calibrations = owner.calibrations.without(iq_selected)
                    raise
                result = result.merge(measured)
                if owner._receivers.get("wifi") is not None:
                    owner._receivers.get("wifi").invalidate_calibration(antennas=iq_selected)
            if wifi_selected:
                from .csi_scoped_calibration import calibrate_full_array

                uniform_channel = len({(dict(current[a].settings)["primary"], dict(current[a].settings)["secondary"]) for a in wifi_selected}) == 1
                if wifi_selected == set(owner.antennas) and uniform_channel:
                    # Keep the full-array CSI correction and clock association
                    # used by backlogs and WiFi TX schedules, from one capture.
                    measured = calibrate_full_array(owner.wifi, per_board=per_board, duration=duration, cable_lengths=owner._geometry.cables, cable_velocity_factors=owner._geometry.velocity_factors)
                else:
                    measured = owner.wifi.calibrate_channels(
                        antennas=wifi_selected,
                        per_board=per_board,
                        duration=duration,
                        run_in_thread=not owner.wifi.processing,
                        cable_lengths=None if per_board else owner._geometry.cables,
                        cable_velocity_factors=None if per_board else owner._geometry.velocity_factors,
                    )
                result = result.merge(measured)
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
        self._receivers = {}
        self._tx = None
        self._monitor = None
        self._iq_tx_boards = set()
        self._wifi_tx_boards = set()
        self._reference_boards = set()
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
            self.calibrations = Calibration()
            for board in self.boards:
                board.reboot()

    def _refresh_receivers(self, configs=None):
        """Snapshot operating roles outside the callback thread."""
        if not self._receivers:
            return
        if configs is None:
            configs = [b.iq.get_config() if b.api_version[0] >= 4 else {"modes": ["wifi"] * 8} for b in self.boards]
        for name, receiver in tuple(self._receivers.items()):
            mode = self._receiver_factories[name][1]
            if mode is None:
                continue
            expected = np.zeros((len(self.boards), 2, 4), dtype=bool)
            for i, (board, config) in enumerate(zip(self.boards, configs)):
                for antenna, role in enumerate(config["modes"]):
                    expected[i, *board.revision.antenna_id_to_row_col(antenna)] = role == mode
            receiver._expected = expected

    _receiver_factories = {}
    _receiver_factories_lock = RLock()

    @staticmethod
    def register_configuration(mode, planner):
        """Register a read-only planner returning a prepared ConfigurationUpdate."""
        from .configuration import register_configuration
        register_configuration(mode, planner)

    @classmethod
    def register_receiver(cls, name, factory, *, sensor_mode=None):
        """Register a lazy pool-owned receiver, like Board.register_capability.

        The factory takes this Pool and returns a ClusterAssembler. The pool
        owns processing and subscription cleanup. Registration never changes
        hardware. sensor_mode optionally supplies the active-antenna mask.
        """
        if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
            raise ValueError("Receiver name must be a public Python identifier")
        if not callable(factory):
            raise TypeError("Receiver factory must be callable")
        with cls._receiver_factories_lock:
            previous = cls._receiver_factories.get(name)
            entry = factory, sensor_mode
            if previous is not None and previous != entry:
                raise ValueError(f"Receiver {name!r} is already registered")
            cls._receiver_factories[name] = entry

    def get_receiver(self, name):
        """Get a receiver without changing sensor modes or radio configuration."""
        with self._lock:
            with self._receiver_factories_lock:
                entry = self._receiver_factories.get(name)
            if entry is None:
                raise KeyError(f"Unknown receiver {name!r}")
            self.open()
            if name not in self._receivers:
                receiver = entry[0](self)
                self._receivers[name] = receiver
                try:
                    self._refresh_receivers()
                except Exception:
                    self._receivers.pop(name)
                    receiver._close()
                    raise
            return self._receivers[name]

    @property
    def iq(self):
        return self.get_receiver("iq")

    @property
    def wifi(self):
        return self.get_receiver("wifi")

    @property
    def ble(self):
        return self.get_receiver("ble")

    @property
    def tx(self):
        with self._lock:
            self.open()
            if self._tx is None or self._tx.closed:
                self._tx = IQTransmitter(self)
            return self._tx

    def refresh_calibration(self, timeout=5.0):
        """Retain IQ calibration after same-frequency RX/TX mode switches.

        Read fresh sample origins and apply the corresponding TX correction.
        RX capture briefly uses interval mode, then restores its trigger.
        Configure radios through this pool while using retained calibration.
        Changes from another client or direct board calls require recalibration.
        """
        from ._iq_calibration.refresh import refresh

        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        with self._lock:
            self.open()
            if not self.calibrations.groups:
                return self.calibrations
            return refresh(self, timeout)

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
        """Process all instantiated receivers in one background thread.

        Receivers created later participate automatically. Callbacks should
        return promptly, since a slow callback delays the other receive paths.
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
        for receiver in tuple(self._receivers.values()):
            if self._closed:
                break
            if receiver is not None:
                count += receiver._run(timeout=0)
        return count

    def run(self, timeout=0.005):
        """Process all instantiated receivers once, for programs without a worker.

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
            for b in self._iq_tx_boards:
                attempt(lambda b=b: _rpc(b, "set_iq_tx", {"muted": True}))
        # Receive-only pools must not change unrelated transmitters or REF sources.
        for b in self._wifi_tx_boards:
            attempt(lambda b=b: b.wifi_tx.set_config({"active_by_antid": [False] * 8}))
        for b in self._reference_boards:
            attempt(b.iq.set_reference)
        for pool in tuple(self._receivers.values()):
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
