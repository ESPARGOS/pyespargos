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
- array-wide coarse time sync (one WiFi reference packet timestamped by all
  sensors anchors all chunk grids, :meth:`sync`);
- reference CW tone control, routed to the master board (the only transmitter
  that reaches the shared reference distribution network);
- model-free fine time/phase calibration (:meth:`calibrate`): the reference
  tone is swept across the captured band, the per-bin antenna covariance is
  accumulated over complete clusters, and a per-sensor (tau, phi) complex fit
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

from . import iq_sync
from . import iq_tone
from .iq_accum_cluster import IQAccumCluster
from .iq_cluster import CHUNK_SAMPLES, IQCluster
from .iq_packet import IQAccumPacket
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

# adc_decimation -> complex sample rate (matches adc_dump_sample_cycles in fw)
DECIM_TO_FS = {1: 80e6, 2: 40e6, 4: 20e6, 6: 10e6, 8: 8e6, 10: 4e6}
_CALIBRATION_MAX_RESIDUAL_RAD = 0.30
_SIGNAL_EPOCH_MISMATCH_RECOVERY_THRESHOLD = 3


def iq_receiver_config(config, antenna_id=0):
    """Return one receiver from a canonical controller IQ config."""
    for receiver in config.get("receivers", []):
        if int(receiver.get("antid", -1)) == int(antenna_id):
            return receiver
    raise ValueError(f"IQ config has no receiver entry for antenna {antenna_id}")


def _remove_reference_path_term(correction, reference_path_delays, frequency_hz):
    """Adapt an OTA correction vector for a cabled reference measurement."""

    return np.asarray(correction) * np.exp(2j * np.pi * np.asarray(reference_path_delays) * float(frequency_hz))


def _within_board_phase_residuals(spectra, correction, board_references):
    """Return corrected sensor phases relative to antenna 0 of each board."""

    spectra = np.asarray(spectra)
    correction = np.asarray(correction)
    board_count, rows, columns = spectra.shape[1:]
    residuals = np.zeros((board_count, rows, columns))
    for board_index, ref_row, ref_col in board_references:
        reference = spectra[:, board_index, ref_row, ref_col]
        for row in range(rows):
            for col in range(columns):
                cross = np.sum(spectra[:, board_index, row, col] * np.conj(reference))
                corr = correction[board_index, row, col] * np.conj(correction[board_index, ref_row, ref_col])
                residuals[board_index, row, col] = float(np.angle(cross * corr))
    return residuals


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
        self._last_chunk_monotonic = None

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
                    trigger_config = config.get("trigger_config") or [0, 0]
                    if config.get("mode") != "iq" or config.get("trigger_mode") != 4 or len(trigger_config) < 2 or not trigger_config[1]:
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
        trigger_config = config.get("trigger_config") or [0, 0, 0]
        if config.get("mode") != "iq" or config.get("trigger_mode") != 4 or len(trigger_config) < 3 or not trigger_config[1]:
            # No longer in Signal mode: disarm until Signal traffic returns.
            self._signal_last_signal_monotonic = None
            return
        if quiet < trigger_config[2] / 1000.0 + _SIGNAL_WATCHDOG_QUIET_SECONDS:
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
            # A drop by more than half the 24-bit range is a wrap (or a sync
            # re-fire counter reset) -> next epoch.
            if self._unwrap_last is not None and raw + (1 << 23) < self._unwrap_last:
                self._unwrap_epoch += 1
            self._unwrap_last = raw
            self._last_chunk_monotonic = time.monotonic()
            return (self._unwrap_epoch << 24) + raw

    def _create_cluster(self, cache_name, cluster_key, board_index, first_message) -> IQCluster:
        if cache_name == _CACHE_ACCUMULATIONS:
            return IQAccumCluster(
                first_message.payload,
                self.board_revisions,
                gain_phase_compensation=self._gain_phase_enabled,
            )
        return IQCluster(
            cluster_key,
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

    def enter_iq_mode(self, timeout=25.0):
        """Enter IQ mode and wait for every sensor's fresh grid fire.

        This is the counterpart to :meth:`restore_wifi` for applications that
        acquire timestamp anchors while the array is still in WiFi mode.  It
        prevents the UI from reporting readiness while sensors are still in
        the provisional, unsynchronised IQ start-up epoch.
        """

        baselines = [None] * len(self.boards)
        baseline_deadline = time.monotonic() + min(float(timeout), 10.0)
        while any(baseline is None for baseline in baselines) and time.monotonic() < baseline_deadline:
            for index, board in enumerate(self.boards):
                if baselines[index] is not None:
                    continue
                try:
                    baselines[index] = self._sensor_sync_state(board)
                except Exception:
                    pass
            if any(baseline is None for baseline in baselines):
                time.sleep(0.1)
        incomplete = [self.boards[index].host for index, baseline in enumerate(baselines) if baseline is None]
        if incomplete:
            raise RuntimeError("incomplete sensor diagnostics before entering IQ mode: " + ", ".join(incomplete))
        self.apply_config({"mode": "iq"})
        self._wait_for_fresh_iq_sync(
            baselines,
            [True] * len(self.boards),
            timeout=float(timeout),
        )

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
        payload = {"enable": bool(enable), "backoff_qdb": int(backoff_qdb)}
        if enable:
            payload["freq_khz"] = int(round(freq_hz / 1e3))
        try:
            self.master_board.iq.set_reftx_tone(payload)
        except espargos.EspargosHTTPStatusError as error:
            # The controller rejects tone ENABLING outside IQ mode (and clears
            # the tone itself when entering WiFi mode). Disabling an
            # (impossible) tone in WiFi mode is a no-op, not an error.
            if enable or error.status != 409:
                raise

    def set_reference_tone_freq(self, freq_hz, cbw, enable=True, backoff_qdb=30, cap=None):
        """Forced high-band reference tone (above the WiFi grid, via the
        sub-channel-table divider path). With cap=None the VCO cap auto-cal runs
        (non-deterministic up high). With cap in 0..255 the VCO coarse cap is
        FORCED — the tone is then DETERMINISTIC (see
        :data:`.iq_tone.HIGHBAND_CAP_FREQ_MHZ`). The controller releases the
        override when the tone is disabled (so WiFi/CSI is unaffected)."""
        payload = {"enable": bool(enable), "backoff_qdb": int(backoff_qdb), "cbw": int(cbw)}
        if enable:
            payload["freq_khz"] = int(round(freq_hz / 1e3))
            if cap is not None:
                payload["cap"] = int(cap)
        self.master_board.iq.set_reftx_tone_freq(payload)

    def place_highband_tone(self, freq_hz, backoff_qdb=30):
        """Place the forced-cap high-band tone at freq_hz — instant, from the
        hardcoded cap<->frequency tables (no measurement). Returns
        ``(cap, expected_freq_mhz, approximate)``; in the approximate zone the
        tone wanders thermally (read the exact frequency off a spectrum)."""
        cap, f_expected, approximate = iq_tone.highband_cap_for_freq(freq_hz)
        self.set_reference_tone_freq(iq_tone.HIGHBAND_TONE_CODE_HZ, iq_tone.HIGHBAND_TONE_CBW, backoff_qdb=backoff_qdb, cap=cap)
        return cap, f_expected, approximate

    def apply_tone_step(self, step):
        """Key one step of a :func:`.iq_tone.tone_sweep_steps` plan:
        ("freq", f_khz) via the exact WiFi-channel path, ("cap", cap) via the
        forced-VCO-cap path."""
        if step[0] == "cap":
            self.set_reference_tone_freq(iq_tone.HIGHBAND_TONE_CODE_HZ, iq_tone.HIGHBAND_TONE_CBW, cap=step[1])
        else:
            self.set_reference_tone(step[1] * 1e3)

    def get_reference_tone(self) -> dict:
        """Return the master controller's reference tone generator state."""
        return self.master_board.iq.get_reftx_tone()

    # ---- array-wide coarse time sync ----

    @staticmethod
    def _sensor_sync_state(board_obj):
        """Return ``antid -> (uptime_ms, timed_sync_count)`` for a board.

        The controller refreshes these in-band sensor counters about twice a
        second.  Requiring all antenna IDs prevents a temporarily incomplete
        version-info snapshot from being mistaken for sensor readiness.
        """
        sensors = board_obj.control.get_json("get_sensor_debug").get("sensors", [])
        state = {int(sensor["antid"]): (int(sensor["uptime_ms"]), int(sensor["iq_timed_syncs"])) for sensor in sensors if "antid" in sensor and "uptime_ms" in sensor and "iq_timed_syncs" in sensor}
        return state if set(state) == set(range(SENSOR_COUNT)) else None

    @staticmethod
    def _sensor_wifi_state(board_obj):
        """Return ``antid -> (uptime_ms, promiscuous_rx_count)``.

        A freshly increased receive counter proves that the sensor has really
        entered WiFi mode. This matters because the shared controller stream
        may still contain CSI queued before a mode transition; using such a
        stale packet as a time anchor produces a foreign/future MAC-time epoch
        and makes the following IQ grid fire fail closed.
        """
        sensors = board_obj.control.get_json("get_sensor_debug").get("sensors", [])
        state = {int(sensor["antid"]): (int(sensor["uptime_ms"]), int(sensor["wifi_promisc_cb"])) for sensor in sensors if "antid" in sensor and "uptime_ms" in sensor and "wifi_promisc_cb" in sensor}
        return state if set(state) == set(range(SENSOR_COUNT)) else None

    def _wait_for_fresh_wifi_rx(self, baselines, only, timeout):
        """Wait until every selected sensor reports post-transition WiFi RX."""
        selected_sensors = {(board_index, antenna_id) for board_index, selected in enumerate(only) if selected for antenna_id in range(SENSOR_COUNT)}
        pending = set(selected_sensors)
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            pending = set(selected_sensors)
            for board_index, selected in enumerate(only):
                if not selected:
                    continue
                try:
                    current = self._sensor_wifi_state(self.boards[board_index])
                except Exception:
                    continue
                if current is None:
                    continue
                baseline = baselines[board_index]
                for antenna_id in range(SENSOR_COUNT):
                    uptime, count = current[antenna_id]
                    old_uptime, old_count = baseline[antenna_id]
                    ready = count != old_count if uptime >= old_uptime else count != 0
                    if ready:
                        pending.discard((board_index, antenna_id))
            if not pending:
                return
            time.sleep(0.1)
        names = [f"{self.boards[board_index].host}/ant{antenna_id}" for board_index, antenna_id in sorted(pending)]
        raise RuntimeError("timed out waiting for fresh WiFi receive activity after mode " "transition: " + ", ".join(names))

    def _wait_for_fresh_iq_sync(self, baselines, only, timeout, stability_time=0.5):
        """Wait for a fresh grid fire that remains valid on every sensor."""
        selected_sensors = {(board_index, antenna_id) for board_index, selected in enumerate(only) if selected for antenna_id in range(SENSOR_COUNT)}
        pending = set(selected_sensors)
        deadline = time.monotonic() + float(timeout)
        last_errors = {}
        stable_since = None
        while time.monotonic() < deadline:
            pending = set(selected_sensors)
            for board_index, selected in enumerate(only):
                if not selected:
                    continue
                try:
                    current = self._sensor_sync_state(self.boards[board_index])
                except Exception as error:
                    last_errors[board_index] = error
                    continue
                if current is None:
                    continue
                baseline = baselines[board_index]
                for antenna_id in range(SENSOR_COUNT):
                    uptime, count = current[antenna_id]
                    old_uptime, old_count = baseline[antenna_id]
                    # A normal IQ re-entry advances the counter.  If a sensor
                    # rebooted during the transition, its counter restarted;
                    # the first nonzero grid fire is the equivalent proof.
                    if uptime >= old_uptime:
                        delta = (count - old_count) & 0xFFFFFFFF
                        # A reset count must not look like progress when an
                        # in-band uptime snapshot has not yet reflected the
                        # reset. True increments (including wrap) are in the
                        # forward half of the uint32 sequence space.
                        ready = 0 < delta < 0x80000000
                    else:
                        ready = count != 0
                    if ready:
                        pending.discard((board_index, antenna_id))
            if not pending:
                now = time.monotonic()
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= float(stability_time):
                    return
            else:
                # A sensor that fires and then reboots must re-qualify. The
                # old implementation removed it permanently on the first
                # observed counter edge and could return while it was already
                # back at uptime~0 with no synchronized grid.
                stable_since = None
            time.sleep(0.1)
        if not pending:
            pending = set(selected_sensors)
            last_errors[-1] = RuntimeError("fresh grid fire did not remain stable long enough")
        if pending:
            names = [f"{self.boards[board_index].host}/ant{antenna_id}" for board_index, antenna_id in sorted(pending)]
            detail = "; ".join((f"{self.boards[index].host}: {error}" if index >= 0 else str(error)) for index, error in sorted(last_errors.items()))
            suffix = f" ({detail})" if detail else ""
            raise RuntimeError("timed out waiting for sensors to re-enter synchronized IQ mode: " + ", ".join(names) + suffix)

    def sync(self, timeout=15.0, iq_ready_timeout=25.0):
        """Array-wide coarse time sync: ONE reference packet (master REFTX ->
        star splitter -> every sensor of every board) is timestamped by all
        sensors, and per-board anchors derived from that same packet are
        posted to every controller — all chunk grids (and grid indices) then
        coincide array-wide. Anchor timestamps come from WiFi reference
        packets, so boards in IQ mode round-trip through WiFi mode and
        re-enter IQ afterwards (the sensors (re-)fire on the anchored grid at
        IQ entry). The call returns only after every sensor has reported that
        fresh grid fire; ``iq_ready_timeout`` bounds that second wait.
        Blocking (a few seconds); raises on failure. NOTE: a
        re-sync re-rolls the small engine-start offsets, so re-calibrate
        afterwards if fine phase accuracy matters."""
        # A leftover reference CW tone (controller-side state, e.g. from an
        # aborted calibration sweep) suppresses the very reference packets the
        # anchor acquisition waits for. A tone is never legitimate during
        # acquisition, so unconditionally switch it off.
        self.set_reference_tone(enable=False)

        was_iq = []
        for board_obj in self.boards:
            was_iq.append(str(board_obj.iq.get_config().get("mode", "")).lower() == "iq")
        wifi_baselines = [None] * len(self.boards)
        for board_index, selected in enumerate(was_iq):
            if not selected:
                continue
            # A full eight-sensor diagnostic sweep can take several seconds
            # immediately after a simultaneous reboot/OTA. This is readiness,
            # not acquisition latency, so wait long enough for the last SPI
            # slot rather than spuriously failing a valid mode switch.
            baseline_deadline = time.monotonic() + 10.0
            while wifi_baselines[board_index] is None and time.monotonic() < baseline_deadline:
                try:
                    wifi_baselines[board_index] = self._sensor_wifi_state(self.boards[board_index])
                except Exception:
                    pass
                if wifi_baselines[board_index] is None:
                    time.sleep(0.1)
            if wifi_baselines[board_index] is None:
                raise RuntimeError(f"incomplete sensor diagnostics before WiFi transition " f"on {self.boards[board_index].host}")
        baselines = [None] * len(self.boards)
        try:
            if any(was_iq):
                self._logger.info("sync: switching array to WiFi mode (anchors come from WiFi reference packets)")
            errors = self._fanout(lambda board_obj: board_obj.iq.set_config({"mode": "wifi"}), only=was_iq)
            if any(errors):
                raise RuntimeError(f"WiFi-mode round-trip failed: {[str(e) for e in errors if e]}")
            if any(was_iq):
                self._logger.info("sync: waiting for fresh WiFi receive activity on every " "sensor")
                self._wait_for_fresh_wifi_rx(wifi_baselines, was_iq, timeout=min(float(timeout), 15.0))
            # Transient pool: the anchor acquisition needs the clustered-CSI
            # view of the stream; close() detaches its subscriptions again
            # afterwards (the boards' shared streams keep running).
            pool = espargos.CSIPool(self.boards)
            try:
                anchors, seqs = iq_sync.post_sync_anchors(pool, self.boards, timeout=timeout, log=self._logger.info)
            finally:
                pool.close()
        finally:
            # ALWAYS return to IQ mode, also when no anchor packet was caught:
            # leaving the array in WiFi mode would silently stop the whole IQ
            # pipeline of the calling application.
            if any(was_iq):
                self._logger.info("sync: re-entering IQ mode")
            for board_index, selected in enumerate(was_iq):
                if not selected:
                    continue
                baseline_deadline = time.monotonic() + 10.0
                while baselines[board_index] is None and time.monotonic() < baseline_deadline:
                    try:
                        baselines[board_index] = self._sensor_sync_state(self.boards[board_index])
                    except Exception:
                        pass
                    if baselines[board_index] is None:
                        time.sleep(0.1)
                if baselines[board_index] is None:
                    raise RuntimeError(f"incomplete sensor diagnostics before IQ re-entry on " f"{self.boards[board_index].host}")
            reentry_errors = [None] * len(self.boards)
            for attempt in range(3):
                reentry_errors = self._fanout(
                    lambda board_obj: board_obj.iq.set_config({"mode": "iq"}),
                    only=was_iq,
                )
                if not any(reentry_errors):
                    break
                failed = [f"{self.boards[index].host}: {error}" for index, error in enumerate(reentry_errors) if error is not None]
                self._logger.warning(f"IQ re-entry attempt {attempt + 1}/3 failed: " + "; ".join(failed))
                if attempt != 2:
                    time.sleep(0.5)
            if any(reentry_errors):
                failed = [f"{self.boards[index].host}: {error}" for index, error in enumerate(reentry_errors) if error is not None]
                raise RuntimeError("IQ re-entry failed on " + "; ".join(failed))
            if any(was_iq):
                self._wait_for_fresh_iq_sync(baselines, was_iq, timeout=iq_ready_timeout)
        self.clear_chunks()
        self._logger.info(f"array-wide anchored time sync posted ({len(self.boards)} board(s), sync_seq {seqs})")
        return seqs

    # ---- stream resilience ----

    def reconnect(self):
        """Restart the board transports after a silently died stream (the
        stream loop exits on prolonged data gaps; Board.start() reconnects
        cleanly)."""
        self._logger.info("reconnecting boards after stream death")
        for board_obj in self.boards:
            try:
                board_obj.stop()
            except Exception:
                pass
            try:
                board_obj.start()
            except Exception as e:
                self._logger.warning(f"board restart failed for {board_obj.host}: {e}")

    def ensure_stream_alive(self, tries=3):
        """Check that IQ chunks are flowing (requires the processing worker or
        an external :meth:`run` driver); reconnect the boards if not."""
        for _ in range(tries):
            with self._unwrap_lock:
                self._last_chunk_monotonic = None
            time.sleep(3)
            with self._unwrap_lock:
                if self._last_chunk_monotonic is not None:
                    return True
            self.reconnect()
        return False

    # ---- fine time/phase calibration ----

    @property
    def calibration(self) -> SensorCalibration | None:
        """Return the stored fine calibration, or None."""
        return self._calibration

    @property
    def calibrating(self) -> bool:
        """Whether :meth:`calibrate` is running: the array then receives the
        reference tone instead of its antennas."""
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

    def collect_complete_clusters(self, seconds, min_peak=10, epochs=None):
        """Collect complete clusters for a dwell window via a temporary
        callback (any concurrently running consumers are unaffected). Returns
        ``[(chunk_index, iq array (boards, rows, columns, samples)), ...]``
        filtered to a minimum per-sensor AC amplitude. The capture epochs of
        the collected clusters are added to the set ``epochs`` if given."""
        collected = []

        def cb(cluster):
            collected.append((cluster.chunk_index, cluster.iq))
            if epochs is not None:
                epochs.add(cluster.fire_time_ns.tobytes())

        handle = self.add_iq_callback(cb)
        time.sleep(seconds)
        self.remove_iq_callback(handle)
        out = []
        for chunk_index, iq in collected:
            ac = iq - np.mean(iq, axis=-1, keepdims=True)
            if float(np.min(np.max(np.abs(ac), axis=-1))) >= min_peak:
                out.append((chunk_index, iq))
        return out

    def calibrate(
        self,
        center_hz=None,
        rx_gain=40,
        restore=True,
        attempts=2,
        cable_lengths=None,
        cable_velocity_factors=None,
    ):
        """Model-free fine time/phase calibration of the whole (possibly
        multi-board) array — works at ANY center frequency with ANY reference
        waveform. The sensors are coarsely time-synced, so at each FFT bin the
        array snapshot is x_a(f) = h_a(f)·s(f): the unknown (noisy/drifting)
        source s(f) is a common scalar that cancels in the cross-spectrum
        z_a(f) = <x_a x_ref*> ∝ h_a h_ref*. Sweeping the master's reference
        tone across the captured band illuminates every bin; a single
        2-parameter complex fit exp(j(phi + 2 pi f tau)) per sensor (tau
        maximizes the magnitude-weighted coherence, phi is the residual
        angle) then gives the per-sensor offsets — no unwrapping, no per-tone
        frequency measurement, robust on every sensor.

        The result is stored as a generic :class:`espargos.SensorCalibration`
        (timing offset + phase offset per sensor, reference-path trace delays
        compensated by the calibration model). Fresh two-tone probes validate
        every sensor and, on multi-board arrays, the cross-board links; a wrong
        result is never stored silently.

        The capture configuration is not disturbed apart from the RF switch
        and a fixed manual gain (both restored afterwards): trigger and
        decimation settings stay exactly as configured — the per-tone dwell
        adapts to the chunk rate they produce, so a live display keeps
        scrolling at its normal rate during the sweep.

        Calibration RELIES ON the existing coarse time sync (:meth:`sync` —
        enforced at application startup; sync and calibration are separate
        user actions) and does not refresh it, with two exceptions where a
        fresh sync is unavoidable: an explicit retune (calibrating at a
        different center re-rolls the sample alignment) and the retry after a
        FAILED attempt (a stuck sync epoch is the common failure cause, and
        retrying inside it could never succeed).

        ``cable_lengths`` and ``cable_velocity_factors`` describe the
        per-board reference-signal distribution cables. When supplied, their
        delays are included in the reference-path model so that the resulting
        correction applies to over-the-air measurements.

        Blocking (a few seconds) — run from a worker thread; requires chunk
        processing (:meth:`start_processing` is called implicitly). Raises
        :class:`IQCalibrationError` on failure."""
        self.start_processing()
        last_error = None
        self._calibrating = True
        try:
            for attempt in range(attempts):
                try:
                    self._calibrate_once(
                        center_hz=center_hz,
                        rx_gain=rx_gain,
                        sync_first=attempt > 0,
                        cable_lengths=cable_lengths,
                        cable_velocity_factors=cable_velocity_factors,
                    )
                    return self._calibration
                except IQCalibrationError as error:
                    last_error = error
                    self._calibration = None
                    retrying = attempt + 1 < attempts
                    self._logger.warning(f"calibration attempt {attempt + 1} failed: {error}" + (", retrying with fresh sync" if retrying else ""))
            raise last_error
        finally:
            if restore:
                self._restore_after_calibration()
            self._calibrating = False

    def _restore_after_calibration(self):
        if getattr(self, "_calibration_restore_config", None):
            try:
                self.apply_config(self._calibration_restore_config)
            except Exception as e:
                self._logger.warning(f"could not restore pre-calibration config: {e}")

    def _calibrate_once(
        self,
        center_hz=None,
        rx_gain=40,
        sync_first=False,
        cable_lengths=None,
        cable_velocity_factors=None,
    ):
        t_start = time.time()
        board_count = len(self.boards)
        flat_count = board_count * SENSOR_COUNT
        n = CHUNK_SAMPLES
        window = np.hanning(n).astype(np.complex64)

        previous = self.get_config()
        previous_rx = iq_receiver_config(previous)
        if center_hz is None:
            center_hz = int(previous_rx.get("rf_freq_hz", 2437000000))
        retunes = int(center_hz) != int(previous_rx.get("rf_freq_hz", 0))
        fs = DECIM_TO_FS.get(int(previous.get("adc_decimation", 1)), 80e6)

        # The capture configuration is left ALONE apart from the RF switch and
        # a fixed manual gain (the measured tone operating point; AGC would
        # pump on the strong cabled tone): trigger and decimation stay as the
        # user configured them, so a live display keeps its familiar rate
        # during the sweep. Both overridden fields are alignment-safe and
        # restored afterwards; a retune only happens if a different center is
        # requested explicitly (and breaks the alignment, hence the sync).
        calibration_config = {"mode": "iq", "rf_switch": 1, "gain_mode": 1, "rx_gain": rx_gain}
        if retunes:
            calibration_config["rf_freq_hz"] = int(center_hz)
        self._calibration_restore_config = {k: previous[k] for k in ("rf_switch",) if k in previous}
        self._calibration_restore_config["receivers"] = previous["receivers"]

        # The planner limits spacing to keep delay aliases outside the
        # full fit search window, for single boards as well as larger arrays.
        spacing = 2.5e6
        steps = iq_tone.tone_sweep_steps(center_hz, fs, spacing_hz=spacing)

        # The per-tone dwell adapts to the chunk rate the CURRENT trigger
        # settings produce (interval trigger: fs/256 * burst/period), so each
        # tone yields a handful of complete snapshots whatever the rate. For
        # the event-based trigger modes the rate is signal-dependent and
        # unpredictable; a fixed dwell + the minimum-snapshot check below
        # have to do.
        chunk_rate = None
        if int(previous.get("trigger_mode", 0)) == 0:
            trigger_config = list(previous.get("trigger_config") or [])
            period = int(trigger_config[0]) if len(trigger_config) >= 1 else 0
            burst = int(trigger_config[2]) if len(trigger_config) >= 3 else 1
            if period > 0 and burst > 0:
                chunk_rate = fs / CHUNK_SAMPLES * burst / period
        # Four snapshots per tone proved too optimistic in live camera use:
        # an unlucky short dwell can fit a convincing but wrong delay slope,
        # which corrupts every downstream beamformer.  Keep enough independent
        # chunks for the fit itself; the fresh probe below then validates the
        # result on data that did not participate in the fit.
        min_dwell, snapshots_per_tone = (0.16, 8.0) if board_count == 1 else (0.20, 10.0)
        dwell = 0.25 if chunk_rate is None else min(1.0, max(min_dwell, snapshots_per_tone / max(chunk_rate, 1e-3)))
        self._logger.info(f"calibration sweep: {len(steps)} tones, dwell {dwell:.2f} s" + (f" ({chunk_rate:.0f} chunks/s at current trigger settings)" if chunk_rate is not None else " (non-interval trigger, rate unknown)"))

        H = np.zeros((n, flat_count, flat_count), np.complex128)
        snapshots = 0
        epochs = set()
        try:
            self.apply_config(calibration_config)
            time.sleep(0.7)
            if sync_first or retunes:  # a retune always needs a fresh sync
                self.sync()
                time.sleep(1.0)
            for step in steps:
                try:
                    self.apply_tone_step(step)
                except Exception:
                    continue
                time.sleep(0.05)
                for _idx, iq in self.collect_complete_clusters(dwell, min_peak=10, epochs=epochs):
                    # one flat (boards*8)-sensor array snapshot; the tone
                    # dominates its bin (rank-1 array signature) while
                    # uncorrelated per-antenna noise averages out
                    X = iq.reshape(flat_count, n)
                    X = X - np.mean(X, axis=-1, keepdims=True)
                    F = np.fft.fftshift(np.fft.fft(X * window, axis=-1), axes=-1)  # (flat, n)
                    H += np.einsum("as,bs->sab", F, np.conj(F))
                    snapshots += 1
            self.set_reference_tone(enable=False)

            if snapshots < 8:
                raise IQCalibrationError(f"only {snapshots} complete array snapshots collected (trigger rate too low or not firing on the reference tone?)")
            if len(epochs) != 1:
                raise IQCalibrationError("the sensors re-fired during the sweep (capture configuration changed?)")

            # Reference sensor: board 0, firmware antenna id 0.
            ref_row, ref_col = self.boards[0].revision.antenna_id_to_row_col(0)
            ref_flat = ref_row * constants.ANTENNAS_PER_ROW + ref_col

            f = (np.arange(n) - n // 2) / n
            H00 = np.real(np.einsum("sii->si", H)[:, ref_flat])
            trust = (H00 / (np.median(H00[H00 > 0]) + 1e-12)) > 3.0
            trust[n // 2 - 1 : n // 2 + 2] = False  # DC / LO leakage never trusted
            if trust.sum() < 6:
                raise IQCalibrationError(f"only {int(trust.sum())} illuminated bins")
            ff = f[trust]
            taus = np.arange(-16.0, 16.0, 0.01)  # covers worst coarse-sync skew
            basis = np.exp(-2j * np.pi * np.outer(ff, taus))  # (n_trust, n_tau)

            tau_samples = np.zeros(flat_count)
            phi = np.zeros(flat_count)
            fit_coherence = np.ones(flat_count)
            for a in range(flat_count):
                if a == ref_flat:
                    continue
                z = H[trust, a, ref_flat]  # complex, mag-weighted
                coherence = np.abs(z @ basis)  # (n_tau,)
                best = int(np.argmax(coherence))
                tau_samples[a] = float(taus[best])
                fit_coherence[a] = float(coherence[best] / (np.sum(np.abs(z)) + 1e-24))
                phi[a] = float(np.angle(np.sum(z * np.exp(-2j * np.pi * ff * tau_samples[a]))))

            # The fitted response of a sensor sampling LATE by dt has phase
            # slope -2 pi f dt, i.e. tau_samples = -dt*fs — flip the sign so
            # timing_offsets carry the CSI-calibration meaning (positive =
            # sensor runs late). The reference-path delays (PCB traces and,
            # when configured, board-distribution cables) stay IN the fitted
            # values; SensorCalibration compensates them on the absolute
            # frequency grid, exactly like the WiFi calibration.
            sensor_shape = (board_count, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)
            calibration = SensorCalibration(
                sensor_shape=sensor_shape,
                timing_offsets=(-tau_samples / fs).reshape(sensor_shape),
                phase_offsets=phi.reshape(sensor_shape),
                phase_reference_frequency=float(center_hz),
                clock_scope=espargos.ClockReferenceScope.POOL,
                reference_path_delays=compute_reference_path_delays(
                    self.boards,
                    board_cable_lengths=cable_lengths,
                    board_cable_vfs=cable_velocity_factors,
                ),
            )

            self._calibration = calibration
            self._calibration_epoch = epochs.pop()
            self._calibration_sample_rate = fs
            self._calibration_center_frequency = float(center_hz)
            tau_rel = tau_samples - tau_samples[ref_flat]
            self._logger.info(
                f"calibration fitted in {time.time()-t_start:.1f} s: {snapshots} snapshots, "
                f"{int(trust.sum())}/{n} bins, tau {np.min(tau_rel):+.2f}..{np.max(tau_rel):+.2f} smp, "
                f"fit coherence {np.min(fit_coherence):.3f}..{np.max(fit_coherence):.3f}"
            )

            # Validate EVERY sensor against fresh reference-tone snapshots.
            # Applying the calibration with its OTA-only trace compensation
            # removed must flatten a cabled probe within each board.  This
            # catches the intermittent plausible-looking but wrong fit that
            # otherwise breaks FFT, MUSIC, and any other spatial processor in
            # exactly the same way.
            sensor_residuals = self._probe_sensor_residuals(center_hz, fs, dwell)
            if sensor_residuals is None:
                raise IQCalibrationError("self-check probe tone not measurable")
            worst_sensor_residual = max(abs(value) for residuals in sensor_residuals.values() for value in residuals.reshape(-1))
            for f_probe, residuals in sensor_residuals.items():
                self._logger.info(f"self-check @{f_probe/1e6:.0f} MHz: corrected within-board residuals {np.array2string(residuals, precision=3, suppress_small=True)} rad")
            if worst_sensor_residual > _CALIBRATION_MAX_RESIDUAL_RAD:
                raise IQCalibrationError(f"self-check worst within-board residual {worst_sensor_residual:+.3f} rad")

            # Fresh two-tone probe self-check on the cross-board links (the
            # within-board math is the proven single-board estimator; the
            # links are what can silently go wrong). A delay error scales with
            # frequency, a phase error is flat — two tones discriminate both.
            if board_count > 1:
                residuals = self._probe_link_residuals(center_hz, fs, dwell)
                if residuals is None:
                    raise IQCalibrationError("self-check probe tone not measurable")
                worst = max(abs(r) for by_board in residuals.values() for r in by_board[1:])
                for f_probe, by_board in residuals.items():
                    self._logger.info(f"self-check @{f_probe/1e6:.0f} MHz: corrected cross-board residuals {['%+.3f' % r for r in by_board[1:]]} rad")
                if worst > 0.3:
                    raise IQCalibrationError(f"self-check worst residual {worst:+.3f} rad")
            self._logger.info(f"calibration OK ({time.time()-t_start:.1f} s total)")
        except IQCalibrationError:
            self._calibration = None
            raise
        finally:
            try:
                self.set_reference_tone(enable=False)
            except Exception:
                pass

    def _probe_sensor_residuals(self, center_hz, fs, dwell):
        """Validate the stored calibration on every sensor using fresh tones.

        The calibration correction is intended for over-the-air data and
        therefore compensates the PCB reference-trace delays.  A probe still
        travelling through those traces is flattened by removing that
        trace-only term from the correction.  Residual phase is measured
        relative to firmware antenna 0 independently on each board, so any
        unmodelled board-distribution cable delay cancels.
        """

        n = CHUNK_SAMPLES
        window = np.hanning(n).astype(np.complex64)
        correction = self.cal_correction(n)
        if correction is None:
            return None
        reference_delays = compute_reference_path_delays(self.boards)
        lo, hi = center_hz - 0.4 * fs, center_hz + 0.4 * fs
        f_lo, f_hi = max(lo, iq_tone.TONE_MIN_HZ), min(hi, iq_tone.TONE_MAX_HZ)
        if f_hi <= f_lo:
            return None

        board_references = []
        for board_index, board_obj in enumerate(self.boards):
            row, col = board_obj.revision.antenna_id_to_row_col(0)
            board_references.append((board_index, row, col))

        out = {}
        try:
            for fraction in (0.27, 0.73):
                f_probe = f_lo + fraction * (f_hi - f_lo)
                self.set_reference_tone(f_probe)
                time.sleep(0.2)
                collected = self.collect_complete_clusters(max(0.4, 2 * dwell), min_peak=10)
                if len(collected) < 4:
                    return None

                accumulated = np.zeros(n)
                spectra = []
                for _idx, iq in collected:
                    x = iq - np.mean(iq, axis=-1, keepdims=True)
                    F = np.fft.fftshift(np.fft.fft(x * window, axis=-1), axes=-1)
                    spectra.append(F)
                    ref_board, ref_row, ref_col = board_references[0]
                    accumulated += np.abs(F[ref_board, ref_row, ref_col])
                accumulated[n // 2 - 2 : n // 2 + 3] = 0
                k = int(np.argmax(accumulated))
                if accumulated[k] < 8 * np.median(accumulated[accumulated > 0]):
                    return None

                actual_frequency = center_hz + (k - n // 2) / n * fs
                # Remove SensorCalibration's OTA-only trace term so a cabled
                # probe should become zero-phase within each board.
                cabled_correction = _remove_reference_path_term(correction[..., k], reference_delays, actual_frequency)
                residuals = _within_board_phase_residuals(np.stack([F[..., k] for F in spectra]), cabled_correction, board_references)
                out[actual_frequency] = residuals
        finally:
            try:
                self.set_reference_tone(enable=False)
            except Exception:
                pass
        return out

    def _probe_link_residuals(self, center_hz, fs, dwell):
        """Key two fresh probe tones and measure the CALIBRATED cross-board
        phase (board b vs board 0, at each board's firmware-antenna-0
        position) at each. The OTA-only reference-path compensation is removed
        before evaluating the cabled probe, so the corrected residuals must be
        ~0 even when boards use different distribution cables. Returns
        {f_probe_hz: [residual_b rad]} or None if a probe was not
        measurable."""
        n = CHUNK_SAMPLES
        board_count = len(self.boards)
        window = np.hanning(n).astype(np.complex64)
        lo, hi = center_hz - 0.4 * fs, center_hz + 0.4 * fs
        f_lo, f_hi = max(lo, iq_tone.TONE_MIN_HZ), min(hi, iq_tone.TONE_MAX_HZ)
        if f_hi <= f_lo:
            # band entirely above the WiFi-channel tone reach (rare; high-band
            # centers) — no deterministic probe frequency available
            return None
        ant0_positions = [self.boards[b].revision.antenna_id_to_row_col(0) for b in range(board_count)]
        correction = self.cal_correction(n)
        out = {}
        try:
            for frac in (0.3, 0.75):
                f_probe = f_lo + frac * (f_hi - f_lo)
                self.set_reference_tone(f_probe)
                time.sleep(0.2)
                # the probe needs a handful of clusters at the untouched
                # trigger rate: reuse the sweep's rate-adapted dwell, widened
                collected = self.collect_complete_clusters(max(0.3, 2 * dwell), min_peak=10)
                if len(collected) < 4:
                    return None
                accumulated = np.zeros(n)
                spectra = []  # per collected cluster: (board_count, n) at the antenna-0 positions
                for _idx, iq in collected:
                    x = np.stack([iq[b][ant0_positions[b]] for b in range(board_count)])
                    x = x - np.mean(x, axis=-1, keepdims=True)
                    F = np.fft.fftshift(np.fft.fft(x * window, axis=-1), axes=-1)
                    spectra.append(F)
                    accumulated += np.abs(F[0])
                accumulated[n // 2 - 2 : n // 2 + 3] = 0
                k = int(np.argmax(accumulated))
                if accumulated[k] < 8 * np.median(accumulated[accumulated > 0]):
                    return None
                actual_frequency = center_hz + (k - n // 2) / n * fs
                cabled_correction = _remove_reference_path_term(
                    correction[..., k],
                    self._calibration.reference_path_delays,
                    actual_frequency,
                )
                residuals = [0.0]
                for b in range(1, board_count):
                    row, col = ant0_positions[b]
                    row0, col0 = ant0_positions[0]
                    corr = cabled_correction[b, row, col] * np.conj(cabled_correction[0, row0, col0])
                    cross = np.sum([F[b, k] * np.conj(F[0, k]) for F in spectra])
                    residuals.append(float(np.angle(cross * corr)))
                out[actual_frequency] = residuals
        finally:
            try:
                self.set_reference_tone(enable=False)
            except Exception:
                pass
        return out
