#!/usr/bin/env python

"""IQ capture, synchronization and receive calibration for a Pool.

Interval chunks are grouped by synchronized sample index. Signal captures use
capture IDs and offsets. Callbacks receive complete clusters by default, or
partial clusters when their predicate allows them.

Reference calibration aligns the sample grids and measures phase and delay,
including REF path compensation. The 24-bit chunk counter is unwrapped into
monotonic indices within each sample grid, including across counter wraps.
"""

from collections import OrderedDict
import threading
import time

import numpy as np

import espargos
from espargos import constants
from espargos.board import EspargosHTTPStatusError
from espargos.clustering import ClusterAssembler
from espargos.sensor_calibration import SensorCalibration, compute_reference_path_delays
from espargos.calibration import Calibration

from ._iq_calibration import reference as iq_reference
from . import iq_tone
from .iq_accum_cluster import IQAccumCluster
from .iq_rx_stream import IQ_RX_STREAM_RATES, IQStreamBlock, IQStreamPacket, receive_sample_rate
from .iq_cluster import CHUNK_SAMPLES, IQCluster
from .iq_packet import DECIM_TO_FS, IQ_SYNC_INFO_GRID_SYNCED, IQAccumPacket
from .iq_signal_capture import IQSignalAssembler

__all__ = ["DECIM_TO_FS", "IQCalibrationError", "IQReceiver", "SENSOR_COUNT", "iq_receiver_config"]

_CACHE_STREAM = "stream"
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
    """Returned by :meth:`IQReceiver.add_signal_capture_callback`; accepted by
    :meth:`IQReceiver.remove_signal_capture_callback` and exposes the pool's
    central ``signal_assembler`` for diagnostics."""

    def __init__(self, callback, assembler):
        self.callback = callback
        self.signal_assembler = assembler


class _BoardSignalTransport:
    """Validate and retire events in one controller's capture-ID namespace."""

    def __init__(self, receiver, board):
        self.receiver = receiver
        self.board = board
        self._logger = receiver._logger
        self._signal_recovery_lock = threading.Lock()
        self._signal_epoch_mismatch_streak = 0
        self._signal_recovery_running = False
        self._signal_recovery_not_before = 0.0
        self.last_chunk_at = None
        self._signal_watchdog_stop = threading.Event()
        self._signal_watchdog_thread = None
        self.auto_maintenance = True
        self._signal_pending_ack = None
        self.assembler = IQSignalAssembler(on_rejected_event=self._signal_event_rejected)

    def acknowledge(self, generation, capture_id, context):
        """Best-effort automatic retirement without killing pool processing."""

        if self._signal_pending_ack is not None:
            pending_generation, pending_id, _ = self._signal_pending_ack
            if generation == pending_generation and IQSignalAssembler._forward(pending_id, capture_id):
                generation, capture_id, context = self._signal_pending_ack

        for attempt in range(3):
            try:
                self.board.iq.acknowledge_signal_capture(int(generation), int(capture_id))
                self._signal_pending_ack = None
                return True
            except Exception as error:
                if attempt == 2:
                    self._signal_pending_ack = (generation, capture_id, context)
                    self._signal_ack_retry_at = time.monotonic() + 0.2
                    self._logger.warning(
                        "Could not retire Signal event %d:%d after %s: %s",
                        generation,
                        capture_id,
                        context,
                        error,
                    )
                    return False
                time.sleep(0.01 * (attempt + 1))

    def retry_pending(self):
        pending = self._signal_pending_ack
        if pending is not None and time.monotonic() >= self._signal_ack_retry_at:
            self.acknowledge(*pending)

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

        board_iq = self.board.iq
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

    def start_watchdog(self):
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
            if not self.receiver.processing or not self.auto_maintenance:
                continue  # a coordinator holds ACKs deliberately
            stamp = self.last_chunk_at
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
            config = self.board.iq.get_config()
        except Exception:
            return
        trigger = config.get("trigger", {})
        if config.get("mode") != "iq" or trigger.get("mode") != "signal" or not trigger.get("antennas"):
            # No longer in Signal mode: disarm until Signal traffic returns.
            self.last_chunk_at = None
            return
        if quiet < trigger["holdoff_ms"] / 1000.0 + _SIGNAL_WATCHDOG_QUIET_SECONDS:
            return  # a long configured holdoff is legitimate dead time
        if not config.get("boot_released", False):
            return  # controller-held start barrier in progress
        if config.get("boot_line_high", False):
            # Armed with the shared net high: the RF environment is genuinely
            # quiet. Restamp so the next check needs another full quiet window.
            self.last_chunk_at = time.monotonic()
            return
        # Signal mode, holdoff long expired, zero chunk traffic, and a sensor
        # holds the shared BOOT net low: the fail-closed barrier is wedged
        # (e.g. an ACK lost in transit, or a local capture fault). Reset it.
        self._logger.warning(
            "Signal capture pipeline silent for %.1f s with the BOOT net held " "low; resetting the array-wide capture barrier",
            quiet,
        )
        self._signal_try_rearm("liveness watchdog")
        self.last_chunk_at = time.monotonic()

    def _signal_event_rejected(self, generation, capture_id, reason):
        """Internal assembler rejection hook: transport hygiene + recovery."""
        self.acknowledge(generation, capture_id, f"rejection ({reason})")
        if self.auto_maintenance:
            self._signal_capture_rejected(reason)

    def feed(self, cluster):
        """Validate one board-local chunk and retire completed captures."""
        for capture in self.assembler.add_cluster(cluster):
            if self.auto_maintenance:
                self._signal_capture_accepted()
                generations = np.unique(np.asarray(capture.config_generation, dtype=np.uint32))
                self.acknowledge(int(generations[0]), int(capture.capture_id), "acceptance")
            for consumer in list(self.receiver._signal_capture_consumers):
                consumer(capture)

    def close(self):
        self._signal_watchdog_stop.set()
        if self._signal_watchdog_thread is not None and self._signal_watchdog_thread is not threading.current_thread():
            self._signal_watchdog_thread.join()


class IQReceiver(ClusterAssembler):
    """Manage IQ capture and assemble synchronized chunk clusters.

    :param owner: The owning Pool. Obtain this receiver through ``pool.iq``.
    :param chunk_settle_timeout: Seconds an incomplete cluster may wait for
        stragglers before it is offered to ordinary raw-IQ partial-snapshot
        consumers.
    :param signal_settle_timeout: Seconds a Signal chunk offset may wait for
        all eight sensors on its board. On expiry the host can retire the
        incomplete event and release its slot in the sensor snapshot queue.
    :param master_index: Index of the master board (the one whose reference TX
        feeds the shared reference network); autodetected when None
    :param gain_phase_compensation: Correct deterministic phase jumps caused
        by changes between analog gain states (enabled by default)
    """

    def __init__(
        self,
        owner,
        chunk_settle_timeout=15.0,
        signal_settle_timeout=2.0,
        master_index=None,
        gain_phase_compensation=True,
    ):
        super().__init__(owner)
        self._expected = np.ones(self.shape, dtype=bool)
        self._recent_stream_blocks = OrderedDict()
        # One second of keys even if every sensor runs an independent grid.
        self._stream_history_limit = max(1, len(self.boards)) * SENSOR_COUNT * (max(IQ_RX_STREAM_RATES) // IQStreamPacket.SAMPLE_COUNT)
        self.chunk_settle_timeout = float(chunk_settle_timeout)
        self.signal_settle_timeout = float(signal_settle_timeout)
        self._master_index = master_index
        self._gain_phase_enabled = bool(gain_phase_compensation)

        # A synchronized grid shares one 24-bit counter across antennas.
        # Before sync, or after a single-sensor reboot, counters advance independently.
        self._unwrap_lock = threading.Lock()
        self._unwrap_latest = OrderedDict()
        self._unwrap_limit = 4 * (1 + len(self.boards) * SENSOR_COUNT)

        self._calibration: SensorCalibration | None = None
        # capture parameters the stored calibration was computed at (needed to
        # place its corrections on an absolute frequency grid)
        self._calibration_sample_rate = None
        self._calibration_center_frequency = None
        self._calibration_epoch = None
        self._calibration_grid_origin = None
        self._calibrating = False
        self._reference_lock = threading.Lock()
        self._reference_sequence = time.monotonic_ns() & 0xFFFFFFFF

        for board_index, board_obj in enumerate(self.boards):
            iq = board_obj.iq
            self._subscribe_sensor_messages(board_index, iq, iq.subscribe_stream)
            self._subscribe_sensor_messages(board_index, iq, iq.subscribe_chunks)
            self._subscribe_sensor_messages(board_index, iq, iq.subscribe_accumulations)

        self._signal_capture_consumers = []
        self._signal_transports = [_BoardSignalTransport(self, board) for board in self.boards]
        self.add_callback(self._feed_signal, callback_predicate=self._signal_ready)
        for transport in self._signal_transports:
            transport.start_watchdog()

    @staticmethod
    def _signal_ready(cluster):
        board = cluster.signal_board_index
        return board is None or cluster.settled or np.all(cluster.completion[board])

    def _feed_signal(self, cluster):
        board = cluster.signal_board_index
        if board is not None:
            self._signal_transports[board].feed(cluster._signal_board_view())

    # ---- data plane: chunk clustering ----

    def _complete(self, cluster):
        expected = self._expected
        return bool(np.any(expected) and np.all(cluster.completion[expected]))

    def add_callback(self, callback, callback_predicate=None, include_partial=False):
        """Register a callback for assembled :class:`.IQCluster` objects.

        By default only clusters containing every antenna currently in IQ RX mode are delivered, immediately on completion. With
        ``include_partial``, settled incomplete clusters are delivered as well
        (immediately on a local grid, after ``chunk_settle_timeout`` on a shared grid) —
        such consumers must tolerate out-of-order chunk indices between the
        immediate complete and the delayed partial deliveries. A custom
        ``callback_predicate`` overrides both behaviors; it can consult
        ``cluster.settled`` to recognize the settled-incomplete case.

        :param callback: The function to call, gets the :class:`.IQCluster`
        :param callback_predicate: Cluster completion predicate, see :meth:`espargos.clustering.ClusterAssembler.add_cluster_callback`
        :param include_partial: Also deliver settled incomplete clusters (ignored if ``callback_predicate`` is given)
        :return: A callback handle for :meth:`remove_callback`
        """
        predicate = callback_predicate
        if predicate is None:
            predicate = (lambda cluster: cluster.settled or self._complete(cluster)) if include_partial else self._complete

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

    def add_stream_callback(self, callback, *, include_partial=False):
        """Receive aligned narrowband blocks. Partial blocks retain NaNs for missing antennas."""

        def ready(block):
            return not isinstance(block, IQStreamBlock) or self._complete(block) or (include_partial and block.settled)

        def deliver(block):
            if isinstance(block, IQStreamBlock):
                callback(block)

        return self.add_cluster_callback(deliver, callback_predicate=ready)

    def remove_callback(self, callback) -> bool:
        """Remove a callback previously returned by :meth:`add_callback`."""
        return self.remove_cluster_callback(callback)

    def acknowledge_signal_capture(self, capture):
        """Release a retained Signal event after host-side validation."""

        if len(self.boards) != 1:
            raise ValueError("explicit Signal acknowledgement requires one board")
        generations = np.unique(np.asarray(capture.config_generation, dtype=np.uint32))
        if generations.size != 1:
            raise ValueError("Signal capture has inconsistent configuration generations")
        self.boards[0].iq.acknowledge_signal_capture(int(generations[0]), int(capture.capture_id))

    def _expire_cluster_caches(self):
        super()._expire_cluster_caches()
        if not self.calibrating:
            for transport in self._signal_transports:
                transport.retry_pending()

    def add_signal_capture_callback(self, callback, *, auto_acknowledge=True):
        """Register for complete, contiguous signal-triggered array events.

        The callback receives :class:`.IQSignalCapture`. Signal transport
        maintenance — event assembly, acknowledgement of accepted and
        rejected events, stall recovery and the liveness watchdog — is a
        pool-level duty that runs regardless of registered consumers, so
        applications that only consume the raw chunk stream need no
        Signal-specific code at all.

        This callback requires a single-board pool. The eight antennas on
        that array capture together, but separate arrays trigger independently.
        A shared clock and reference do not synchronize their Signal events.
        Use Interval or Accumulate for synchronized multi-array capture.

        Set ``auto_acknowledge=False`` to manage accepted captures yourself.
        Rejected events are still retired, but automatic stall recovery is
        disabled. Call :meth:`acknowledge_signal_capture` after validating each
        accepted capture. This option does not coordinate triggers across arrays.
        """

        if len(self.boards) != 1:
            raise ValueError("Signal capture consensus is local to one board's shared BOOT net; " "use one IQReceiver per board")
        transport = self._signal_transports[0]
        if not auto_acknowledge:
            transport.auto_maintenance = False
        self._signal_capture_consumers.append(callback)
        return _SignalCaptureCallbackHandle(callback, transport.assembler)

    def remove_signal_capture_callback(self, callback) -> bool:
        """Remove a consumer by its handle or by the original callback."""
        target = getattr(callback, "callback", callback)
        try:
            self._signal_capture_consumers.remove(target)
            return True
        except ValueError:
            return False

    def _close(self):
        for transport in self._signal_transports:
            transport.close()
        super()._close()

    def add_accumulation_callback(self, callback, callback_predicate=None, include_partial=False):
        """Register for coverage-consistent vectors from all current IQ receivers.

        A custom predicate may opt into deadline-partial or coverage-mismatched
        windows for diagnostics. ``include_partial`` also delivers settled
        incomplete array clusters, analogous to :meth:`add_callback`.
        """

        def complete(cluster):
            # TX/WiFi sensors have no IQ sections in a mixed-mode array.
            counts = cluster.counts[self._expected]
            hashes = cluster.coverage_hashes[self._expected]
            return self._complete(cluster) and bool(np.all(counts == counts[0]) and np.all(hashes == hashes[0]))

        predicate = callback_predicate
        if predicate is None:
            if include_partial:
                predicate = lambda cluster: cluster.settled or complete(cluster)
            else:
                predicate = complete

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
        self._clear_cluster_cache(_CACHE_STREAM)
        self._clear_cluster_cache(_CACHE_CHUNKS)
        self._clear_cluster_cache(_CACHE_SIGNAL_CHUNKS)
        self._clear_cluster_cache(_CACHE_ACCUMULATIONS)
        with self._unwrap_lock:
            self._unwrap_latest.clear()

    def _get_cluster_cache_name(self, board_index, sensor_message) -> str:
        payload = sensor_message.payload
        if isinstance(payload, IQStreamPacket):
            return _CACHE_STREAM
        if isinstance(payload, IQAccumPacket):
            return _CACHE_ACCUMULATIONS
        if payload.is_signal_capture and payload.capture_id != 0xFFFFFFFF:
            self._signal_transports[board_index].last_chunk_at = time.monotonic()
            return _CACHE_SIGNAL_CHUNKS
        return _CACHE_CHUNKS

    def _get_cluster_key(self, board_index, sensor_message) -> int:
        p = sensor_message.payload
        if isinstance(p, IQStreamPacket):
            # Free-running counters are local to an individual antenna.
            local = None if p.synchronized else (board_index, sensor_message.antenna_id, p.fire_time_ns)
            generation = p.config_generation if len(self.boards) == 1 else None
            return (p.sync_info, generation, local, p.center_freq_hz, p.sample_rate_hz, p.first_sample)
        if isinstance(sensor_message.payload, IQAccumPacket):
            payload = sensor_message.payload
            return (
                int(payload.sync_info) & 0xFFFF,
                int(payload.source_chunk_start),
                int(payload.source_chunk_end),
                int(payload.vector_chunks),
                int(payload.sample_rate_hz),
                int(payload.center_freq_hz),
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
        if payload.sync_info & IQ_SYNC_INFO_GRID_SYNCED:
            domain = (payload.sync_info & 0xFFFF, payload.sample_rate_hz)
        else:
            domain = (board_index, sensor_message.antenna_id, payload.fire_time_ns)
        with self._unwrap_lock:
            # Keep the newest observation in each domain. An out-of-order
            # pre-wrap packet maps to the previous turn of that same counter.
            newest = self._unwrap_latest.get(domain, raw)
            delta = (raw - newest + (1 << 23)) % (1 << 24) - (1 << 23)
            index = newest + delta
            self._unwrap_latest[domain] = max(index, newest)
            self._unwrap_latest.move_to_end(domain)
            if len(self._unwrap_latest) > self._unwrap_limit:
                self._unwrap_latest.popitem(last=False)
            return index

    def _resolve_cluster_key(self, cache_name, proposed_key, cache, board_index, sensor_message):
        if cache_name == _CACHE_STREAM and proposed_key in self._recent_stream_blocks:
            return None
        if cache_name == _CACHE_CHUNKS:
            payload = sensor_message.payload
            # A local engine restart can reuse an index while old chunks are
            # still in flight. Only a synchronized grid names the same sample
            # across boards, whose configuration counters are independent.
            if not payload.sync_info & IQ_SYNC_INFO_GRID_SYNCED:
                session = (board_index, sensor_message.antenna_id, payload.fire_time_ns, payload.config_generation)
            elif len(self.boards) == 1:
                session = (board_index, payload.config_generation)
            else:
                session = None
            return (payload.sync_info & 0xFFFF, payload.sample_rate_hz, session, proposed_key)
        return proposed_key

    def _on_cluster_removed(self, cache_name, cluster_key, sensor_cluster):
        if cache_name == _CACHE_STREAM:
            # A delayed duplicate after delivery must not open a second block
            # for the same antennas and sample index.
            self._recent_stream_blocks[cluster_key] = None
            if len(self._recent_stream_blocks) > self._stream_history_limit:
                self._recent_stream_blocks.popitem(last=False)

    def _on_cluster_cache_cleared(self, cache_name):
        if cache_name == _CACHE_STREAM:
            self._recent_stream_blocks.clear()

    def _create_cluster(self, cache_name, cluster_key, board_index, first_message) -> IQCluster:
        if cache_name == _CACHE_STREAM:
            block = IQStreamBlock(first_message.payload, self.board_revisions)
            block.settled = not block.synchronized
            return block
        if cache_name == _CACHE_ACCUMULATIONS:
            return IQAccumCluster(
                first_message.payload,
                self.board_revisions,
                gain_phase_compensation=self._gain_phase_enabled,
            )
        cluster = IQCluster(
            cluster_key[-1] if cache_name == _CACHE_CHUNKS else cluster_key,
            self.board_revisions,
            gain_phase_compensation=self._gain_phase_enabled,
        )

        if cache_name == _CACHE_CHUNKS:
            cluster.settled = not (first_message.payload.sync_info & IQ_SYNC_INFO_GRID_SYNCED)
        elif cache_name == _CACHE_SIGNAL_CHUNKS:
            cluster.signal_board_index = board_index
        return cluster

    def _on_cluster_updated(self, cache_name, cluster_key, sensor_cluster) -> bool:
        all_callbacks_fired = self._try_callbacks(sensor_cluster)
        # A free-running antenna is the sole member of its local grid. Holding
        # its partial record for the array settle timeout cannot add a peer.
        if sensor_cluster.settled:
            return True
        # TX/WiFi antennas cannot complete an IQ cluster in a mixed-mode pool.
        if cache_name == _CACHE_SIGNAL_CHUNKS:
            return all_callbacks_fired and self._signal_ready(sensor_cluster)
        return all_callbacks_fired and self._complete(sensor_cluster)

    def _on_cluster_expired(self, cache_name, cluster_key, sensor_cluster) -> None:
        # A settled incomplete cluster: mark it settled and offer it to the
        # callbacks whose predicates also accept partial snapshots (each
        # callback fires at most once per cluster, so consumers already
        # served on completion are not called again).
        sensor_cluster.settled = True
        self._try_callbacks(sensor_cluster)

    def _get_cluster_cache_timeout(self, cache_name: str) -> float | None:
        if cache_name == _CACHE_STREAM:
            return min(0.15, self.chunk_settle_timeout)
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

    def _invalidate_rx_phases(self, antennas):
        if self._calibration is None:
            return
        for antenna in antennas:
            bi = self._owner.hosts.index(antenna.host)
            row, col = self.boards[bi].revision.antenna_id_to_row_col(antenna.antid)
            self._calibration.phase_offsets[bi, row, col] = np.nan

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
                    if not 0 <= value <= 2:
                        raise ValueError("gain_mode must be auto, manual, or expert")
                    value = ("auto", "manual", "expert")[value]
                receiver_update[canonical_key] = value
        if receiver_update:
            if "receivers" in config:
                raise ValueError("cannot combine array-wide receiver fields with receivers")
            config["receivers"] = [{"antid": antenna_id, **receiver_update} for antenna_id in range(SENSOR_COUNT)]

        invalid = set()
        # Gain and antenna routing preserve the sample epoch.
        if self._calibration is not None or self._owner.calibrations.groups:
            from .calibration import Antenna

            shared = {
                "capture_path",
                "bluetooth_sample_rate_hz",
                "bluetooth_filter_khz",
                "adc_decimation",
                "adc_source_sel",
                "bw_mhz",
                "second_chan",
                "filter_bw_mhz",
                "loopback",
                "tx_tone_enable",
                "tx_tone0_step",
                "loopback_tx_gain",
                "loopback_rx_gain",
                "loopback_bb_gain",
            }
            tuning = [r for r in config.get("receivers", []) if "rf_freq_hz" in r]
            if shared.intersection(config) or {"trigger", "mode", "modes"}.intersection(config) or tuning:
                for board in self.boards:
                    old = board.iq.get_config()
                    updated = old | config
                    if "bluetooth_filter_khz" in config and old.get("bluetooth_filter_steps_khz"):
                        khz = config["bluetooth_filter_khz"]
                        updated["bluetooth_filter_khz"] = min(old["bluetooth_filter_steps_khz"], key=lambda step: (abs(step - khz), step))
                    if updated.get("capture_path") == "bluetooth":
                        active_fields = {"capture_path", "bluetooth_sample_rate_hz", "bluetooth_filter_khz"}
                    else:
                        active_fields = shared - {"bluetooth_sample_rate_hz", "bluetooth_filter_khz"}
                    if 0 < updated.get("filter_bw_mhz", 0) < 13:
                        updated["filter_bw_mhz"] = 13
                    if "trigger" in config and receive_sample_rate(old) != receive_sample_rate(updated) or any(updated[k] != old.get(k) for k in active_fields.intersection(config)):
                        invalid.update(Antenna(board.host, a) for a, mode in enumerate(old["modes"]) if mode == "iq_rx")
                    modes = old["modes"]
                    if "mode" in config:
                        modes = ["iq_rx" if config["mode"] in ("iq", True) else "wifi"] * SENSOR_COUNT
                    for a, mode in enumerate(config.get("modes", modes)):
                        if mode != old["modes"][a]:
                            invalid.add(Antenna(board.host, a))
                    for r in tuning:
                        previous = iq_receiver_config(old, r["antid"])
                        if r["rf_freq_hz"] != previous["rf_freq_hz"]:
                            invalid.add(Antenna(board.host, r["antid"]))

        errors = self._fanout(lambda board_obj: board_obj.iq.set_config(dict(config)))
        if invalid:
            # Firmware validates before applying a 400/409 response. Keep that
            # board's measured phases. A timeout or 5xx can follow a physical
            # change, so those boards must be invalidated along with successes.
            rejected = {b.host for b, error in zip(self.boards, errors) if isinstance(error, EspargosHTTPStatusError) and error.status in (400, 409)}
            changed = {antenna for antenna in invalid if antenna.host not in rejected}
            if changed:
                self._owner.calibrations = self._owner.calibrations.without(changed)
                self._invalidate_rx_phases(changed)
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
    def master_index(self) -> int | None:
        """Index of the master board — the one whose reference TX may generate
        the shared reference (wificonf calib-mode != Never); kit slaves are
        Never. A controller outside the aperture has no board index."""
        if self._owner.reference_controller is not None:
            return None
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
        if self._owner.reference_controller is not None:
            return self._owner.reference_controller
        return self.boards[self.master_index]

    def set_reference_tone(self, freq_hz=None, enable=True, backoff_qdb=30):
        """Reference CW tone on the cabled distribution network (master
        controller ESP32). While active, reference packet TX is suppressed;
        disabling restores the packet-TX WiFi state."""
        board = self.master_board
        if enable:
            self._owner._reference_boards.add(board)
        board.iq.set_reference("cw" if enable else "off", freq_hz, backoff_qdb)
        if not enable:
            self._owner._reference_boards.discard(board)

    def place_highband_tone(self, freq_hz, backoff_qdb=30):
        """Place the forced-cap high-band tone at freq_hz — instant, from the
        hardcoded cap<->frequency tables (no measurement). Returns
        ``(cap, expected_freq_mhz, approximate)``; in the approximate zone the
        tone wanders thermally (read the exact frequency off a spectrum)."""
        cap, f_expected, approximate = iq_tone.highband_cap_for_freq(freq_hz)
        self._owner._reference_boards.add(self.master_board)
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
        ``source`` (an :class:`.IQCluster`, :class:`.IQStreamBlock`, :class:`.IQAccumCluster` or
        :class:`.IQSignalCapture`).

        A restarted sample engine has a new epoch. Same-frequency IQ mode
        switches through the pool transfer the correction to that epoch.
        Unhandled restarts and retunes remain detectable as a mismatch."""
        if self._calibration is None or self._calibration_epoch is None:
            return False
        if isinstance(source, IQStreamBlock) and not source.synchronized:
            return False
        fire = np.asarray(source.fire_time_ns, dtype=np.uint64)
        phases = self._calibration.phase_offsets
        if fire.shape != phases.shape:
            return False
        present = getattr(source, "completion", None)
        if present is None:  # A published Signal capture includes every sensor.
            present = np.ones(fire.shape, dtype=bool)
        if not np.any(present) or not np.all(np.isfinite(phases[present])):
            return False
        epochs = np.frombuffer(self._calibration_epoch, np.uint64).reshape(fire.shape)
        centers = np.broadcast_to(source.center_freq_hz, fire.shape)
        rates = np.broadcast_to(source.sample_rate_hz, fire.shape)
        return bool(np.array_equal(epochs[present], fire[present]) and np.all(centers[present] == self._calibration_center_frequency) and np.all(rates[present] == self._calibration_sample_rate))

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

    def sync(self, center_hz=None, rx_gain=None, attempts=5, cable_lengths=None, cable_velocity_factors=None, timeout=3.0, iq_ready_timeout=3.0):
        """Calibrate IQ receivers and return the dense receive model.

        Prefer ``pool.calibrate()`` for antenna selections and mixed RX/TX roles.
        This low-level entry point retains the receive-only API used by older
        applications. It shares the pool's calibration lock.
        """
        from .calibration import Antenna
        from ._iq_calibration.scope import CalibrationScope

        with self._owner._lock:
            antennas = [Antenna(b.host, a) for b in self.boards for a, mode in enumerate(b.iq.get_config()["modes"]) if mode == "iq_rx"]
            scope = CalibrationScope(self, antennas)
            configs = [b.iq.get_config() for b in self.boards]
            self._owner.calibrations._check_replacement((Antenna(self.boards[bi].host, a), "rx", center_hz or configs[bi]["receivers"][a]["rf_freq_hz"]) for bi, a in scope.pairs)
            try:
                return self._sync(center_hz, rx_gain, attempts, cable_lengths, cable_velocity_factors, timeout, iq_ready_timeout, scope=scope, publish=True)
            except Exception:
                self._owner.calibrations = self._owner.calibrations.without(antennas)
                raise

    def _sync(self, center_hz=None, rx_gain=None, attempts=5, cable_lengths=None, cable_velocity_factors=None, timeout=3.0, iq_ready_timeout=3.0, *, scope=None, publish=False):
        """Synchronize and calibrate from one broadband REF session.

        Acquire a common sample grid, fit phase/delay, validate on fresh data,
        then restore receive settings and publish the calibration atomically.
        By default, choose RX gain for the configured internal or external
        reference path. Pass ``rx_gain`` to override acquisition gain. The
        Bluetooth phase measurement uses the operating receiver gains unless
        explicitly overridden. Retries vary the reference waveform and power.
        """
        t0 = time.monotonic()
        if attempts < 1:
            raise ValueError("attempts must be positive")
        from ._iq_calibration.scope import CalibrationScope

        scope = scope or CalibrationScope(self)
        configs = [b.iq.get_config() for b in self.boards]
        preserve_grid = scope.check_sync(configs)
        # Separate boards can have independently established local grids.
        # Only reuse a local grid for a single board, or a required subgroup.
        warm_grid = len(scope.active) == 1 and scope.has_grid(configs)
        bi, a = scope.pairs[0]
        row, col = self.boards[bi].revision.antenna_id_to_row_col(a)
        ref = bi * 8 + row * 4 + col
        streaming = configs[bi].get("trigger", {}).get("mode") == "downsample"
        bluetooth = configs[bi].get("capture_path") == "bluetooth" or streaming
        profile = (bluetooth, streaming, configs[bi].get("bluetooth_filter_khz") if bluetooth else None)
        for idx in scope.active:
            downsample = configs[idx].get("trigger", {}).get("mode") == "downsample"
            bt = configs[idx].get("capture_path") == "bluetooth" or downsample
            if (bt, downsample, configs[idx].get("bluetooth_filter_khz") if bt else None) != profile:
                raise IQCalibrationError("Select the same capture path, bandwidth and sampling mode on participating boards")
        coarse_gain = iq_reference.reference_gain(self, rx_gain) if bluetooth else rx_gain
        keep_bluetooth_epoch = bluetooth and warm_grid
        with iq_reference.reference_session(self, center_hz, coarse_gain, scope, keep_bluetooth_epoch) as (center, fs):
            timing_center = iq_reference.BLUETOOTH_TIMING_CENTER_HZ if bluetooth else center
            self._calibration = None
            for attempt in range(attempts):
                try:
                    # The transport can time out while a controller services a
                    # firmware update. A new REF acquisition needs a live data
                    # subscription even when its HTTP requests still succeed.
                    for board_index in scope.active:
                        board = self.boards[board_index]
                        if not board.is_stream_connected:
                            board.start(transports=self._owner._transports)
                    if bluetooth and attempt:
                        scope.apply_rx({"capture_path": "wifi", "adc_decimation": 1, "adc_source_sel": 15, "loopback": 0, "tx_tone_enable": 0, "gain_mode": "manual", "rx_gain": coarse_gain, "rf_freq_hz": timing_center})
                    if keep_bluetooth_epoch and attempt == 0:
                        seqs = None
                    elif preserve_grid or (warm_grid and attempt == 0):
                        self.master_board.iq.set_reference("iq", timing_center, iq_reference.REFERENCE_BACKOFF)
                        scope.apply_rx({"trigger": {"mode": "interval", "interval_chunks": 8192, "burst_chunks": 4}})
                        seqs = None
                    else:
                        seqs = iq_reference.acquire_grid(self, timing_center, timeout, scope)
                    if bluetooth:
                        from ._iq_calibration.stream import measure_bluetooth

                        trigger = configs[bi]["trigger"] if streaming else {"mode": "interval", "interval_chunks": max(64, round(fs / 256 / 100)), "burst_chunks": 4}
                        scope.apply_rx({"capture_path": "bluetooth", "rf_freq_hz": center, "trigger": trigger})
                        timing, phi, epoch, grid_origin = measure_bluetooth(self, scope, configs, center, fs, iq_ready_timeout, attempt, rx_gain)
                        break
                    training = iq_reference.collect(self, count=32, timeout=iq_ready_timeout, seqs=seqs, mask=scope.mask)
                    tau, phi, quality = iq_reference.fit(training, ref, scope.mask)
                    validation = iq_reference.collect(self, count=32, timeout=iq_ready_timeout, seqs=seqs, mask=scope.mask)
                    tau2, phi2, _ = iq_reference.fit(validation, ref, scope.mask)
                    if not np.array_equal(training[0].fire_time_ns[scope.mask], validation[0].fire_time_ns[scope.mask]):
                        raise IQCalibrationError("Capture epoch changed during calibration")
                    residual = np.abs(np.angle(np.exp(1j * (phi2 - phi)))) + np.pi * np.abs(tau2 - tau) * (36e6 / 80e6)
                    if np.nanmax(residual) > _CALIBRATION_MAX_RESIDUAL_RAD:
                        raise IQCalibrationError(f"Broadband validation residual {np.nanmax(residual):.3f} rad")
                    break
                except RuntimeError as error:
                    if attempt + 1 == attempts:
                        raise IQCalibrationError(str(error)) from error
                    self._logger.info("Acquiring a fresh broadband grid: %s", error)
            if not bluetooth:
                timing = tau / 80e6
                epoch = np.where(scope.mask, training[0].fire_time_ns, 0).astype(np.uint64).tobytes()
                grid_origin = training[0].grid_origin_ns.copy()
            if not bluetooth and fs != 80e6:
                # The ADC subsamples without anti-alias filtering. Measure
                # the full reference band at 80 MS/s, then transfer the delay
                # using the measured starts of the two capture epochs.
                scope.apply_rx({"adc_decimation": {rate: decimation for decimation, rate in DECIM_TO_FS.items()}[fs], "trigger": {"mode": "interval", "interval_chunks": max(128, 2 ** round(np.log2(fs / 256 / 100))), "burst_chunks": 4}})
                self.clear_chunks()
                target = iq_reference.collect(self, count=4, seqs=seqs, mask=scope.mask)[0]
                timing = iq_reference.transfer_delay(timing, training[0].grid_origin_ns, target.grid_origin_ns, ref)
                epoch = np.where(scope.mask, target.fire_time_ns, 0).astype(np.uint64).tobytes()
                grid_origin = target.grid_origin_ns.copy()
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
        self._calibration_grid_origin = grid_origin
        self._calibration_sample_rate = fs
        self._calibration_center_frequency = center
        if bluetooth:
            self._logger.info("Narrowband calibration verified at %d Sa/s in %.3f s", fs, time.monotonic() - t0)
        else:
            self._logger.info("Broadband calibration verified: coherence %.3f, residual %.3f rad, %.3f s", np.nanmin(quality), np.nanmax(residual), time.monotonic() - t0)
        from ._iq_calibration.orchestration import snapshot

        if publish:
            result = snapshot(self, scope=scope)
            self._owner.calibrations = self._owner.calibrations.merge(result)
        return calibration

    # Both names run the same operation; there is no separate coarse-only path.
    calibrate = sync

    def calibrate_tx(self, **kwargs):
        """Calibrate configured IQ roles through the owning pool.

        Boards share a clock and a forward RF reference network. The
        procedure synchronizes RX first, then measures interlaced TX pilots.
        User waveforms and settings are restored. Failure leaves TX muted.
        See :mod:`espargos._iq_calibration.orchestration`.
        """
        method = kwargs.pop("method", "reciprocity")
        if kwargs.keys() - {"path"}:
            raise TypeError("Use pool.calibrate() and configure reference cables on the pool")
        return self._owner.calibrate(tx_method=method, **kwargs)
