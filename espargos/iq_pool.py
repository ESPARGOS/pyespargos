#!/usr/bin/env python

"""Pool for raw synchronized IQ sampling — the IQ counterpart of ``CSIPool``.

:class:`IQPool` receives the boards' IQ chunk streams and assembles ordinary
raw chunks with equal synchronized indices into an :class:`.IQCluster`.
Signal-mode chunks instead use the firmware's wired-consensus capture ID and
offset; their per-sensor SRAM counters remain diagnostic metadata.
Clusters are handed to registered callbacks: complete clusters immediately,
incomplete ones once they have settled (SPI uplink drops mean some chunks
never arrive; consumers that also want those partial snapshots register a
callback predicate that accepts them).

Beyond the data plane, the pool manages the whole IQ-mode lifecycle, like
``CSIPool`` does for Wi-Fi:

- capture configuration fan-out (``set_iq_control``) with the compact
  array-wide receiver-field translation;
- reference CW tone control, routed to the master board (the only transmitter
  that reaches the shared reference distribution network);
- combined synchronization and time/phase calibration (:meth:`sync`): the
  broadband onset aligns the grids, then illuminates the captured band;
  coherent cross-spectra and a per-sensor (tau, phi) complex fit
  yields a generic :class:`espargos.SensorCalibration` — the same calibration
  model the CSI stack uses, with the reference-path (PCB trace) delays
  compensated by the calibration class itself.

The sensor-side chunk index is only 24 bits (wraps every ~54 s at decimation
1) and resets to ~0 on every sync re-fire; the pool unwraps it into a
monotonic 64-bit cluster key, because consumers key on it (ordering, FFT-block
assembly) and a backwards jump would poison any newest-index bookkeeping.
"""

import threading
import time

import numpy as np

import espargos
from espargos import constants
from espargos.pool import Pool
from espargos.sensor_calibration import SensorCalibration, compute_reference_path_delays

from . import iq_reference
from . import iq_tone
from .iq_accum_cluster import IQAccumCluster
from .iq_cluster import CHUNK_SAMPLES, IQCluster
from .iq_packet import DECIM_TO_FS, IQAccumPacket
from .iq_signal_capture import IQSignalAssembler

__all__ = ["DECIM_TO_FS", "IQCalibrationError", "IQPool", "SENSOR_COUNT", "iq_receiver_config"]

_CACHE_CHUNKS = "chunks"
_CACHE_SIGNAL_CHUNKS = "signal_chunks"
_CACHE_ACCUMULATIONS = "accumulations"

# Quiet window (seconds, beyond the configured holdoff) after which the
# Signal liveness watchdog inspects the array. Well above the bounded
# per-event transport time (~0.2 s) and the Signal settle timeout.
_SIGNAL_WATCHDOG_QUIET_SECONDS = 5.0

SENSOR_COUNT = 8

_CALIBRATION_MAX_RESIDUAL_RAD = 0.30
_SIGNAL_EPOCH_MISMATCH_RECOVERY_THRESHOLD = 3


def iq_receiver_config(config, antenna_id=0):
    """Return one receiver from a canonical controller IQ config."""
    for receiver in config.get("receivers", []):
        if int(receiver.get("antid", -1)) == int(antenna_id):
            return receiver
    raise ValueError(f"IQ config has no receiver entry for antenna {antenna_id}")


class IQCalibrationError(RuntimeError):
    """Raised when the IQ fine time/phase calibration fails."""


class _SignalCaptureCallbackHandle:
    """Returned by :meth:`IQPool.add_signal_capture_callback`; accepted by
    :meth:`IQPool.remove_signal_capture_callback` and exposes the pool's
    central ``signal_assembler`` for diagnostics."""

    def __init__(self, callback, assembler):
        self.callback = callback
        self.signal_assembler = assembler


class IQPool(Pool):
    """Manage IQ capture and assemble synchronized chunk clusters.

    :param boards: ESPARGOS boards forming one coherent array (a single board,
        or e.g. an Aperture Kit sharing one clock and phase reference)
    :param chunk_settle_timeout: Seconds an incomplete cluster may wait for
        stragglers before it is offered to ordinary raw-IQ partial-snapshot
        consumers.
    :param signal_settle_timeout: Seconds a Signal chunk offset may wait for
        all eight sensors. Signal admits only one event until the host accepts
        or rejects it, so its shorter bounded transport deadline is also the
        recovery time from a mismatched or lost event identity.
    :param master_index: Index of the master board (the one whose reference TX
        feeds the shared reference network); autodetected when None
    :param gain_phase_compensation: Correct deterministic phase jumps caused
        by changes between analog gain states (enabled by default)
    """

    def __init__(
        self,
        boards,
        chunk_settle_timeout=15.0,
        signal_settle_timeout=2.0,
        master_index=None,
        gain_phase_compensation=True,
    ):
        super().__init__(boards)
        self.chunk_settle_timeout = float(chunk_settle_timeout)
        self.signal_settle_timeout = float(signal_settle_timeout)
        self._master_index = master_index
        self._gain_phase_enabled = bool(gain_phase_compensation)

        # 24-bit chunk-index unwrap state (module docstring); the grid is
        # common to all boards by construction, so one global tracker
        # suffices — inter-board arrival skew (<~ms) is far below the 2^23
        # wrap threshold. Post-sync stragglers from the old time base briefly
        # inflate the epoch, keeping the unwrapped index monotonic through
        # the transition; their spurious clusters age out via the settle
        # timeout.
        self._unwrap_lock = threading.Lock()
        self._unwrap_epoch = 0
        self._unwrap_last = None

        # BOOT can occasionally reach one sensor just after its bank boundary,
        # putting that sensor one whole bank ahead for an event. The assembler
        # catches this before delivery. Coalesce repeated mismatches into one
        # capture-generation reset instead of rejecting every later event
        # until a user happens to change a setting. The IQ engines' anchored
        # sample grids remain synchronized, so a WiFi/REFTX round-trip would be
        # unnecessary and would create a much longer visible pause.
        self._signal_recovery_lock = threading.Lock()
        self._signal_epoch_mismatch_streak = 0
        self._signal_recovery_running = False
        self._signal_recovery_not_before = 0.0

        # Liveness watchdog for SILENT Signal stalls. The drop-based recovery
        # above needs at least one observable rejected event; a stall in which
        # no chunk arrives at all (e.g. a wedged fail-closed barrier) would
        # otherwise persist until a user happens to change a setting.
        self._signal_last_signal_monotonic = None
        self._signal_watchdog_stop = threading.Event()
        self._signal_watchdog_thread = None

        self._calibration: SensorCalibration | None = None
        # capture parameters the stored calibration was computed at (needed to
        # place its corrections on an absolute frequency grid)
        self._calibration_sample_rate = None
        self._calibration_center_frequency = None
        self._calibration_epoch = None
        self._calibrating = False
        self._reference_lock = threading.Lock()
        self._reference_sequence = time.monotonic_ns() & 0xFFFFFFFF

        for board_index, board_obj in enumerate(self.boards):
            iq = board_obj.iq
            self._subscribe_sensor_messages(board_index, iq, iq.subscribe_chunks)
            self._subscribe_sensor_messages(board_index, iq, iq.subscribe_accumulations)

        # Pool-level Signal transport maintenance. The sensors retain every
        # emitted Signal event until the host acknowledges it, so SOME host
        # entity must assemble and acknowledge events even when the
        # application only consumes the raw chunk stream (e.g. a waterfall
        # display) — otherwise the bounded per-sensor snapshot queues fill
        # after four events and the whole array stalls. One internal
        # assembler per (single-board) pool performs that duty, feeds the
        # drop-based recovery and the liveness watchdog, and dispatches
        # complete captures to add_signal_capture_callback() consumers.
        self._signal_capture_consumers = []
        self._signal_auto_maintenance = True
        self._signal_assembler = None
        if len(self.boards) == 1:
            self._signal_assembler = IQSignalAssembler(on_rejected_event=self._signal_event_rejected)
            self.add_iq_callback(
                self._signal_feed,
                callback_predicate=lambda cluster: cluster.is_complete or cluster.settled,
            )
            self._signal_watchdog_start()

    # ---- data plane: chunk clustering ----

    def add_iq_callback(self, callback, callback_predicate=None, include_partial=False):
        """Register a callback for assembled :class:`.IQCluster` objects.

        By default only complete clusters (every sensor of every board
        contributed) are delivered, immediately on completion. With
        ``include_partial``, settled incomplete clusters are delivered as well
        (after ``chunk_settle_timeout``, once no more chunks are expected) —
        such consumers must tolerate out-of-order chunk indices between the
        immediate complete and the delayed partial deliveries. A custom
        ``callback_predicate`` overrides both behaviors; it can consult
        ``cluster.settled`` to recognize the settled-incomplete case.

        :param callback: The function to call, gets the :class:`.IQCluster`
        :param callback_predicate: Cluster completion predicate, see :meth:`espargos.Pool.add_cluster_callback`
        :param include_partial: Also deliver settled incomplete clusters (ignored if ``callback_predicate`` is given)
        :return: A callback handle for :meth:`remove_iq_callback`
        """
        predicate = callback_predicate
        if predicate is None:
            predicate = (lambda cluster: cluster.settled or cluster.is_complete) if include_partial else (lambda cluster: cluster.is_complete)

        # Pool callback completion is tracked globally. Treat clusters of the
        # other IQ data type as an immediate no-op so a raw callback cannot
        # keep accumulation clusters cached forever (and vice versa).
        def typed_callback(cluster):
            if isinstance(cluster, IQCluster):
                callback(cluster)

        return self.add_cluster_callback(
            typed_callback,
            callback_predicate=lambda cluster: not isinstance(cluster, IQCluster) or predicate(cluster),
        )

    def remove_iq_callback(self, callback) -> bool:
        """Remove a callback previously returned by :meth:`add_iq_callback`."""
        return self.remove_cluster_callback(callback)

    def acknowledge_signal_capture(self, capture):
        """Release a retained Signal event after host-side validation."""

        if len(self.boards) != 1:
            raise ValueError("explicit Signal acknowledgement requires one board")
        generations = np.unique(np.asarray(capture.config_generation, dtype=np.uint32))
        if generations.size != 1:
            raise ValueError("Signal capture has inconsistent configuration generations")
        self.boards[0].iq.acknowledge_signal_capture(int(generations[0]), int(capture.capture_id))

    def _auto_acknowledge_signal_identity(self, generation, capture_id, context):
        """Best-effort automatic retirement without killing pool processing."""

        for attempt in range(3):
            try:
                self.boards[0].iq.acknowledge_signal_capture(int(generation), int(capture_id))
                return True
            except Exception as error:
                if attempt == 2:
                    self._logger.warning(
                        "Could not retire Signal event %d:%d after %s: %s",
                        generation,
                        capture_id,
                        context,
                        error,
                    )
                    return False
                time.sleep(0.01 * (attempt + 1))

    def _signal_capture_accepted(self):
        with self._signal_recovery_lock:
            self._signal_epoch_mismatch_streak = 0

    def _signal_capture_rejected(self, reason):
        """Recover fail-closed Signal state without accepting bad data.

        One mismatch can be a transient bank-boundary race and is safely
        dropped. Consecutive mismatches, or a settled incomplete sensor set,
        mean the fail-closed capture barrier needs an array-wide generation
        reset. This is the programmatic equivalent of the settings change that
        used to make a stalled GUI resume; it does not disturb the synchronized
        IQ sample grid.
        """

        epoch_mismatch = reason.startswith("array source epoch mismatch")
        incomplete = reason.startswith("settled incomplete sensor set") or (reason.startswith("Signal event ") and (" incomplete when event " in reason or reason.endswith("exceeded pending-event window")))
        if not epoch_mismatch and not incomplete:
            return
        with self._signal_recovery_lock:
            # Chunks already in UDP/WebSocket/controller buffers can describe
            # the retired generation for a short time after a rearm. Do not
            # turn that harmless tail into a storm of new generations.
            if time.monotonic() < getattr(self, "_signal_recovery_not_before", 0.0):
                return
            if epoch_mismatch:
                self._signal_epoch_mismatch_streak += 1
                if self._signal_epoch_mismatch_streak < _SIGNAL_EPOCH_MISMATCH_RECOVERY_THRESHOLD:
                    return
            else:
                self._signal_epoch_mismatch_streak = 0
            if self._signal_recovery_running:
                return
            self._signal_recovery_running = True
            self._signal_recovery_not_before = time.monotonic() + 2.0

        def recover():
            try:
                if epoch_mismatch:
                    self._logger.warning(
                        "Signal source banks diverged for %d consecutive " "events; resetting the array-wide capture barrier",
                        _SIGNAL_EPOCH_MISMATCH_RECOVERY_THRESHOLD,
                    )
                else:
                    self._logger.warning("Signal event had an incomplete sensor set; resetting " "the array-wide capture barrier")
                # Event validation stays on this host. Ask the controller only
                # to perform the physical all-sensor boundary: hold BOOT low,
                # broadcast a fresh generation, then release the shared net.
                self._signal_try_rearm("dropped-event recovery")
            except Exception as error:
                self._logger.warning("automatic Signal recovery failed: %s", error)
            finally:
                with self._signal_recovery_lock:
                    self._signal_epoch_mismatch_streak = 0
                    self._signal_recovery_running = False

        threading.Thread(
            target=recover,
            name="iq-signal-recovery",
            daemon=True,
        ).start()

    def _signal_try_rearm(self, context) -> bool:
        """Config-checked, retried barrier rearm; True when the rearm was sent.

        ``set_iq_control {"rearm_signal": true}`` is only valid while the
        stored configuration is Signal mode; a mode change racing the recovery
        thread must be a silent skip, not a one-shot failure that leaves a
        genuinely wedged barrier stalled forever.
        """

        board_iq = self.boards[0].iq
        get_config = getattr(board_iq, "get_config", None)
        for attempt in range(3):
            try:
                if get_config is not None:
                    config = get_config()
                    trigger = config.get("trigger", {})
                    if config.get("mode") != "iq" or trigger.get("mode") != "signal" or not trigger.get("antennas"):
                        self._logger.info(
                            "Signal rearm (%s) skipped: array no longer in Signal mode",
                            context,
                        )
                        return False
                board_iq.rearm_signal_capture()
                return True
            except Exception as error:
                if attempt == 2:
                    self._logger.warning(
                        "Signal rearm (%s) failed after 3 attempts: %s",
                        context,
                        error,
                    )
                    return False
                time.sleep(0.5 * (attempt + 1))
        return False

    def _signal_watchdog_start(self):
        if self._signal_watchdog_thread is not None:
            return
        self._signal_watchdog_stop.clear()
        self._signal_watchdog_thread = threading.Thread(
            target=self._signal_watchdog_loop,
            name="iq-signal-watchdog",
            daemon=True,
        )
        self._signal_watchdog_thread.start()

    def _signal_watchdog_loop(self):
        while not self._signal_watchdog_stop.wait(1.0):
            if not self._signal_auto_maintenance:
                continue  # a coordinator holds ACKs deliberately
            stamp = self._signal_last_signal_monotonic
            if stamp is None:
                continue
            quiet = time.monotonic() - stamp
            if quiet < _SIGNAL_WATCHDOG_QUIET_SECONDS:
                continue
            with self._signal_recovery_lock:
                if self._signal_recovery_running or time.monotonic() < (self._signal_recovery_not_before):
                    continue
                self._signal_recovery_running = True
                # Whatever the check concludes, do not re-inspect (or hammer a
                # temporarily unreachable controller) more than every 2 s.
                self._signal_recovery_not_before = time.monotonic() + 2.0
            try:
                self._signal_watchdog_check(quiet)
            finally:
                with self._signal_recovery_lock:
                    self._signal_recovery_running = False

    def _signal_watchdog_check(self, quiet):
        try:
            config = self.boards[0].iq.get_config()
        except Exception:
            return
        trigger = config.get("trigger", {})
        if config.get("mode") != "iq" or trigger.get("mode") != "signal" or not trigger.get("antennas"):
            # No longer in Signal mode: disarm until Signal traffic returns.
            self._signal_last_signal_monotonic = None
            return
        if quiet < trigger["holdoff_ms"] / 1000.0 + _SIGNAL_WATCHDOG_QUIET_SECONDS:
            return  # a long configured holdoff is legitimate dead time
        if not config.get("boot_released", False):
            return  # controller-held start barrier in progress
        if config.get("boot_line_high", False):
            # Armed with the shared net high: the RF environment is genuinely
            # quiet. Restamp so the next check needs another full quiet window.
            self._signal_last_signal_monotonic = time.monotonic()
            return
        # Signal mode, holdoff long expired, zero chunk traffic, and a sensor
        # holds the shared BOOT net low: the fail-closed barrier is wedged
        # (e.g. an ACK lost in transit, or a local capture fault). Reset it.
        self._logger.warning(
            "Signal capture pipeline silent for %.1f s with the BOOT net held " "low; resetting the array-wide capture barrier",
            quiet,
        )
        self._signal_try_rearm("liveness watchdog")
        self._signal_last_signal_monotonic = time.monotonic()

    def _signal_event_rejected(self, generation, capture_id, reason):
        """Internal assembler rejection hook: transport hygiene + recovery."""
        self._auto_acknowledge_signal_identity(generation, capture_id, f"rejection ({reason})")
        if self._signal_auto_maintenance:
            self._signal_capture_rejected(reason)

    def _signal_feed(self, cluster):
        """Feed the pool's central Signal transport maintainer (__init__)."""
        if self._signal_assembler is None:
            return
        if bool(np.all(np.asarray(cluster.capture_id) == 0xFFFFFFFF)):
            return  # ordinary interval traffic carries no Signal identity
        for capture in self._signal_assembler.add_cluster(cluster):
            if self._signal_auto_maintenance:
                self._signal_capture_accepted()
                generations = np.unique(np.asarray(capture.config_generation, dtype=np.uint32))
                self._auto_acknowledge_signal_identity(int(generations[0]), int(capture.capture_id), "acceptance")
            for consumer in list(self._signal_capture_consumers):
                consumer(capture)

    def add_signal_capture_callback(self, callback, *, auto_acknowledge=True):
        """Register for complete, contiguous signal-triggered array events.

        The callback receives :class:`.IQSignalCapture`. Signal transport
        maintenance — event assembly, acknowledgement of accepted and
        rejected events, stall recovery and the liveness watchdog — is a
        pool-level duty that runs regardless of registered consumers, so
        applications that only consume the raw chunk stream need no
        Signal-specific code at all.

        Set ``auto_acknowledge=False`` for a multi-array coordinator: the
        pool then stops acknowledging ACCEPTED events (rejected events are
        still retired) and disables automatic stall recovery; call
        :meth:`acknowledge_signal_capture` explicitly after the wider
        consensus succeeds.
        """

        if len(self.boards) != 1 or self._signal_assembler is None:
            raise ValueError("Signal capture consensus is local to one board's shared BOOT net; " "use one IQPool per board")
        if not auto_acknowledge:
            self._signal_auto_maintenance = False
        self._signal_capture_consumers.append(callback)
        return _SignalCaptureCallbackHandle(callback, self._signal_assembler)

    def remove_signal_capture_callback(self, callback) -> bool:
        """Remove a consumer by its handle or by the original callback."""
        target = getattr(callback, "callback", callback)
        try:
            self._signal_capture_consumers.remove(target)
            return True
        except ValueError:
            return False

    def stop_processing(self):
        self._signal_watchdog_stop.set()
        super().stop_processing()

    def stop(self):
        self._signal_watchdog_stop.set()
        super().stop()

    def add_accumulation_callback(self, callback, callback_predicate=None, include_partial=False):
        """Register for complete, coverage-consistent accumulated vectors.

        A custom predicate may opt into deadline-partial or coverage-mismatched
        windows for diagnostics. ``include_partial`` also delivers settled
        incomplete array clusters, analogous to :meth:`add_iq_callback`.
        """

        predicate = callback_predicate
        if predicate is None:
            if include_partial:
                predicate = lambda cluster: cluster.settled or (cluster.is_complete and cluster.coverage_consistent)
            else:
                predicate = lambda cluster: cluster.is_complete and cluster.coverage_consistent

        def typed_callback(cluster):
            if isinstance(cluster, IQAccumCluster):
                callback(cluster)

        return self.add_cluster_callback(
            typed_callback,
            callback_predicate=lambda cluster: not isinstance(cluster, IQAccumCluster) or predicate(cluster),
        )

    def remove_accumulation_callback(self, callback) -> bool:
        return self.remove_cluster_callback(callback)

    def clear_chunks(self):
        """Drop all pending chunk clusters and reset the index unwrapping.

        Used after a re-sync: the anchored re-fire resets all sensors' chunk
        counters, so pre-sync clusters can never complete and would only
        pollute the store.
        """
        self._clear_cluster_cache(_CACHE_CHUNKS)
        self._clear_cluster_cache(_CACHE_SIGNAL_CHUNKS)
        self._clear_cluster_cache(_CACHE_ACCUMULATIONS)
        with self._unwrap_lock:
            self._unwrap_epoch = 0
            self._unwrap_last = None

    def _get_cluster_cache_name(self, board_index, sensor_message) -> str:
        payload = sensor_message.payload
        if isinstance(payload, IQAccumPacket):
            return _CACHE_ACCUMULATIONS
        if payload.is_signal_capture and payload.capture_id != 0xFFFFFFFF:
            self._signal_last_signal_monotonic = time.monotonic()
            return _CACHE_SIGNAL_CHUNKS
        return _CACHE_CHUNKS

    def _get_cluster_key(self, board_index, sensor_message) -> int:
        if isinstance(sensor_message.payload, IQAccumPacket):
            payload = sensor_message.payload
            return (
                int(payload.config_generation),
                int(payload.source_chunk_start),
                int(payload.vector_chunks),
            )
        payload = sensor_message.payload
        if payload.is_signal_capture and payload.capture_id != 0xFFFFFFFF:
            # Signal association is defined by the wired-BOOT consensus event,
            # so a bad source epoch cannot split one event into misleading
            # partial chunk clusters. IQSignalAssembler independently requires
            # all eight source epochs to agree before it publishes the event;
            # this synthetic, contiguous key is association-only.
            return (1 << 128) | (int(board_index) << 96) | (int(payload.config_generation) << 64) | (int(payload.capture_id) << 32) | int(payload.capture_chunk_offset)
        raw = int(payload.source_chunk_index) & 0x00FFFFFF
        with self._unwrap_lock:
            # Different sensors can straddle a wrap in arrival order. Keep
            # the newest index as the reference; a delayed pre-wrap packet
            # belongs to the previous epoch and must not cause another wrap.
            if self._unwrap_last is None:
                self._unwrap_last = raw
            newest = (self._unwrap_epoch << 24) + self._unwrap_last
            delta = (raw - self._unwrap_last + (1 << 23)) % (1 << 24) - (1 << 23)
            index = newest + delta
            if index > newest:
                self._unwrap_epoch, self._unwrap_last = divmod(index, 1 << 24)
            return index

    def _resolve_cluster_key(self, cache_name, proposed_key, cache, board_index, sensor_message):
        if cache_name == _CACHE_CHUNKS:
            payload = sensor_message.payload
            # A rate change or a new grid can reuse an index while old data
            # is still in transport. Keep those observations separate.
            return (payload.sync_info & 0xFFFF, payload.sample_rate_hz, proposed_key)
        return proposed_key

    def _create_cluster(self, cache_name, cluster_key, board_index, first_message) -> IQCluster:
        if cache_name == _CACHE_ACCUMULATIONS:
            return IQAccumCluster(
                first_message.payload,
                self.board_revisions,
                gain_phase_compensation=self._gain_phase_enabled,
            )
        return IQCluster(
            cluster_key[-1] if cache_name == _CACHE_CHUNKS else cluster_key,
            self.board_revisions,
            gain_phase_compensation=self._gain_phase_enabled,
        )

    def _on_cluster_updated(self, cache_name, cluster_key, sensor_cluster) -> bool:
        all_callbacks_fired = self._try_callbacks(sensor_cluster)
        return all_callbacks_fired and sensor_cluster.is_complete

    def _on_cluster_expired(self, cache_name, cluster_key, sensor_cluster) -> None:
        # A settled incomplete cluster: mark it settled and offer it to the
        # callbacks whose predicates also accept partial snapshots (each
        # callback fires at most once per cluster, so consumers already
        # served on completion are not called again).
        sensor_cluster.settled = True
        self._try_callbacks(sensor_cluster)

    def _get_cluster_cache_timeout(self, cache_name: str) -> float | None:
        if cache_name == _CACHE_SIGNAL_CHUNKS:
            return self.signal_settle_timeout
        return self.chunk_settle_timeout

    @property
    def gain_phase_compensation(self) -> bool:
        """Whether new clusters compensate gain-state phase jumps."""

        return self._gain_phase_enabled

    @gain_phase_compensation.setter
    def gain_phase_compensation(self, enabled: bool) -> None:
        self._gain_phase_enabled = bool(enabled)

    # ---- configuration ----

    def apply_config(self, config: dict):
        """Push (a partial) IQ control config to every board; each controller
        merges it onto its stored config and re-pushes to its sensors.

        Demo controls expose a compact array-wide receiver editor. Translate
        those convenience fields into the canonical eight-receiver API here;
        callers that need independent receivers can pass ``receivers``
        directly or use :meth:`espargos.board_iq.IQCapability.set_receiver`.
        """
        config = dict(config)
        receiver_keys = ("rf_freq_hz", "gain_mode", "rx_gain", "expert_gain_words", "expert_gain_word")
        receiver_update = {}
        for key in receiver_keys:
            if key in config:
                value = config.pop(key)
                canonical_key = "expert_gain_words" if key == "expert_gain_word" else key
                if canonical_key == "gain_mode" and isinstance(value, int):
                    value = ("auto", "manual", "expert")[value]
                receiver_update[canonical_key] = value
        if receiver_update:
            if "receivers" in config:
                raise ValueError("cannot combine array-wide receiver fields with receivers")
            config["receivers"] = [{"antid": antenna_id, **receiver_update} for antenna_id in range(SENSOR_COUNT)]

        errors = self._fanout(lambda board_obj: board_obj.iq.set_config(dict(config)))
        failed = [f"{self.boards[i].host}: {e}" for i, e in enumerate(errors) if e is not None]
        if failed:
            raise RuntimeError("set_iq_control failed on " + "; ".join(failed))

    def get_config(self) -> dict:
        """Return the first board's canonical IQ configuration (the config
        fan-out keeps all boards equal)."""
        return self.boards[0].iq.get_config()

    def restore_wifi(self):
        """Best-effort return of every board to WiFi/CSI mode (teardown path).

        The firmware force-disables the reference tone when a controller
        enters WiFi mode, so no tone handling is needed here.
        """
        try:
            self.apply_config({"mode": "wifi"})
        except Exception as e:
            self._logger.warning(f"could not restore WiFi mode: {e}")

    def enter_iq_mode(self, timeout=3.0):
        """Enter and synchronize IQ sampling; an already running grid is kept."""
        if any(board.iq.get_config()["mode"] != "iq" for board in self.boards):
            self.sync(timeout=timeout)

    def _fanout(self, fn, only=None):
        """Run fn(board) on all (or selected) boards concurrently; returns the
        per-board exceptions (None where fn succeeded)."""
        indices = range(len(self.boards)) if only is None else [i for i, selected in enumerate(only) if selected]
        errors = [None] * len(self.boards)

        def run(i):
            try:
                fn(self.boards[i])
            except Exception as e:
                errors[i] = e

        threads = [threading.Thread(target=run, args=(i,)) for i in indices]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return errors

    # ---- master board / reference tone ----

    @property
    def master_index(self) -> int:
        """Index of the master board — the one whose reference TX may generate
        the shared reference (wificonf calib-mode != Never); kit slaves are
        Never. Autodetected on first access unless given to the constructor."""
        if self._master_index is None:
            self._master_index = 0
            for i, board_obj in enumerate(self.boards):
                try:
                    if int(board_obj.wifi_rx.get_config().get("calib-mode", 0)) != 0:
                        self._master_index = i
                        break
                except Exception:
                    continue
        return self._master_index

    @property
    def master_board(self):
        return self.boards[self.master_index]

    def set_reference_tone(self, freq_hz=None, enable=True, backoff_qdb=30):
        """Reference CW tone on the cabled distribution network (master
        controller ESP32). While active, reference packet TX is suppressed;
        disabling restores the packet-TX WiFi state."""
        self.master_board.iq.set_reference("cw" if enable else "off", freq_hz, backoff_qdb)

    def place_highband_tone(self, freq_hz, backoff_qdb=30):
        """Place the forced-cap high-band tone at freq_hz — instant, from the
        hardcoded cap<->frequency tables (no measurement). Returns
        ``(cap, expected_freq_mhz, approximate)``; in the approximate zone the
        tone wanders thermally (read the exact frequency off a spectrum)."""
        cap, f_expected, approximate = iq_tone.highband_cap_for_freq(freq_hz)
        self.master_board.iq.set_reference("cw", iq_tone.HIGHBAND_TONE_CODE_HZ, backoff_qdb, {"cbw": iq_tone.HIGHBAND_TONE_CBW, "cap": cap})
        return cap, f_expected, approximate

    def get_reference_tone(self) -> dict:
        """Return the master controller's reference tone generator state."""
        return self.master_board.iq.get_reference()

    @property
    def calibration(self) -> SensorCalibration | None:
        """Return the stored fine calibration, or None."""
        return self._calibration

    @property
    def calibrating(self) -> bool:
        """Whether synchronization or calibration is using the broadband REF path."""
        return self._calibrating

    @property
    def calibration_sample_rate(self) -> float | None:
        """Sample rate at which the stored calibration was measured."""

        return self._calibration_sample_rate

    @property
    def calibration_center_frequency(self) -> float | None:
        """Center frequency at which the stored calibration was measured."""

        return self._calibration_center_frequency

    def calibration_applies_to(self, source) -> bool:
        """Whether the stored calibration was measured in the capture epoch of
        ``source`` (an :class:`.IQCluster`, :class:`.IQAccumCluster` or
        :class:`.IQSignalCapture`).

        Every re-fire of the sensors' sample engines (a sync, a retune or a
        sample-rate change) re-rolls the per-sensor sample alignment and
        thereby invalidates the fine calibration. The sensors stamp each chunk
        with the fire time of its epoch, which makes a stale calibration
        detectable."""
        return self._calibration is not None and self._calibration_epoch == np.asarray(source.fire_time_ns, dtype=np.uint64).tobytes()

    def cal_correction(self, fft_size=CHUNK_SAMPLES):
        """Per-bin correction vectors synthesized from the stored calibration
        on the fftshifted baseband grid of the calibration-time capture
        settings: shape ``(boards, rows, columns, fft_size)``, or None if not
        calibrated. Multiplying a sensor's spectrum with its vector removes
        the per-sensor phase/time offset (valid within the calibration's sync
        epoch and until a retune)."""
        if self._calibration is None:
            return None
        bin_frequencies = self._calibration_center_frequency + (np.arange(fft_size) - fft_size // 2) / fft_size * self._calibration_sample_rate
        return self._calibration.phase_time_correction(bin_frequencies)

    def sync(self, center_hz=None, rx_gain=35, attempts=5, cable_lengths=None, cable_velocity_factors=None, timeout=3.0, iq_ready_timeout=3.0):
        """Synchronize and calibrate from one broadband REF session.

        Acquire a common sample grid, fit phase/delay, validate on fresh data,
        then restore receive settings and publish the calibration atomically.
        """
        t0 = time.monotonic()
        if attempts < 1:
            raise ValueError("attempts must be positive")
        row, col = self.boards[0].revision.antenna_id_to_row_col(0)
        ref = row * 4 + col
        with iq_reference.reference_session(self, center_hz, rx_gain) as (center, fs):
            self._calibration = None
            for attempt in range(attempts):
                try:
                    seqs = iq_reference.acquire_grid(self, center, timeout)
                    training = iq_reference.collect(self, count=32, timeout=iq_ready_timeout, seqs=seqs)
                    tau, phi, quality = iq_reference.fit(training, ref)
                    validation = iq_reference.collect(self, count=32, timeout=iq_ready_timeout, seqs=seqs)
                    tau2, phi2, _ = iq_reference.fit(validation, ref)
                    if not np.array_equal(training[0].fire_time_ns, validation[0].fire_time_ns):
                        raise IQCalibrationError("Capture epoch changed during calibration")
                    residual = np.abs(np.angle(np.exp(1j * (phi2 - phi)))) + np.pi * np.abs(tau2 - tau) * (36e6 / 80e6)
                    if residual.max() > _CALIBRATION_MAX_RESIDUAL_RAD:
                        raise IQCalibrationError(f"Broadband validation residual {residual.max():.3f} rad")
                    break
                except RuntimeError as error:
                    if attempt + 1 == attempts:
                        raise IQCalibrationError(str(error)) from error
                    self._logger.info("Acquiring a fresh broadband grid: %s", error)
            timing = tau / 80e6
            epoch = training[0].fire_time_ns.tobytes()
            if fs != 80e6:
                # The ADC subsamples without anti-alias filtering. Measure
                # the full reference band at 80 MS/s, then transfer the delay
                # using the measured starts of the two capture epochs.
                self.apply_config({"adc_decimation": {rate: decimation for decimation, rate in DECIM_TO_FS.items()}[fs], "trigger": {"mode": "interval", "interval_chunks": max(128, 2 ** round(np.log2(fs / 256 / 100))), "burst_chunks": 4}})
                self.clear_chunks()
                target = iq_reference.collect(self, count=4, seqs=seqs)[0]
                timing = iq_reference.transfer_delay(timing, training[0].grid_origin_ns, target.grid_origin_ns, ref)
                epoch = target.fire_time_ns.tobytes()
            shape = (len(self.boards), constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)
            calibration = SensorCalibration(
                sensor_shape=shape,
                timing_offsets=(-timing).reshape(shape),
                phase_offsets=phi.reshape(shape),
                phase_reference_frequency=center,
                clock_scope=espargos.ClockReferenceScope.POOL,
                reference_path_delays=compute_reference_path_delays(self.boards, board_cable_lengths=cable_lengths, board_cable_vfs=cable_velocity_factors),
            )
        # Publish only after validation AND restoration succeeded.
        self._calibration = calibration
        self._calibration_epoch = epoch
        self._calibration_sample_rate = fs
        self._calibration_center_frequency = center
        self._logger.info("Broadband calibration verified: coherence %.3f, residual %.3f rad, %.3f s", quality.min(), residual.max(), time.monotonic() - t0)
        return calibration

    # Both names run the same operation; there is no separate coarse-only path.
    calibrate = sync
