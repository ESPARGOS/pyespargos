"""Pool-owned BLE event assembly, independent of advertisers and visualization."""

from dataclasses import replace
import threading
import numpy as np

from .clustering import ClusterAssembler
from .ble_cluster import BLECluster
from .ble_association import packet_candidates

__all__ = ["BLEReceiver", "BLECalibrationError"]


class BLECalibrationError(RuntimeError):
    """No applicable BLE phase calibration is available."""


class BLEReceiver(ClusterAssembler):
    """Obtain through ``pool.ble`` and process using ``pool.run()`` or its worker.

    Construction subscribes without changing modes, tuning, gain or reference TX.
    All qualified packet types are assembled, including non-advertising PDUs.
    CRC validity remains separate from packet detection.
    Events are board-local. Multiple boards can be observed independently, but
    this receiver does not claim inter-board event or phase synchronization.

    Consumers receive raw BLECluster objects. Phase estimation and calibration
    are explicit operations. Partial clusters and early discovery use ordinary
    cluster callback predicates, just as other receivers do.
    """

    def __init__(self, owner, settle_timeout=1.0, max_clusters=48):
        if settle_timeout <= 0 or max_clusters < 1:
            raise ValueError("Require positive settle timeout and cache limit")
        super().__init__(owner)
        self.settle_timeout = float(settle_timeout)
        self.max_clusters = int(max_clusters)
        self._expected = np.ones(self.shape, bool)
        self._epochs = {}
        self._generations = {}
        self._calibrations = {}
        self._state_lock = threading.RLock()
        self._control_lock = threading.Lock()
        self.dropped = 0
        try:
            for b, board in enumerate(self.boards):
                capability = board.ble
                self._subscribe_sensor_messages(b, capability, capability.subscribe_packets)
        except Exception:
            self._close()
            raise

    def _complete(self, cluster):
        b = cluster.board_index
        expected = self._expected[b]
        return bool(np.any(expected) and np.all(cluster.completion[b][expected]))

    def add_callback(self, callback, callback_predicate=None, *, include_partial=False):
        """Deliver complete events, or also settled partial events when requested.

        A custom predicate overrides that policy. For discovery on the first
        qualified report, use ``lambda cluster: True``. Each callback fires
        once per event. A retained cluster may subsequently acquire more sensors.
        """
        predicate = callback_predicate
        if predicate is None:
            predicate = (lambda c: c.settled or self._complete(c)) if include_partial else self._complete
        return self.add_cluster_callback(callback, predicate)

    def remove_callback(self, callback):
        return self.remove_cluster_callback(callback)

    def _get_cluster_cache_name(self, board_index, sensor_message):
        return "packets"

    def _get_cluster_key(self, board_index, sensor_message):
        p = sensor_message.payload
        return board_index, p.clock_epoch, p.generation, p.settings, p.identity, p.time_us, p.sequence

    def _resolve_cluster_key(self, cache_name, proposed_key, cache, board_index, sensor_message):
        candidates = packet_candidates(cache.values(), board_index, sensor_message)
        if len(candidates) > 1:
            self.dropped += 1
            return None
        return candidates[0].event_key if candidates else proposed_key

    def _create_cluster(self, cache_name, cluster_key, board_index, first_message):
        return BLECluster(cluster_key, board_index, first_message.payload, self.board_revisions)

    def _on_cluster_cached(self, cache_name, cluster_key, cluster):
        cache = self._cluster_caches[cache_name]
        while len(cache) > self.max_clusters:
            cache.pop(next(iter(cache)))
            self.dropped += 1

    def _add_sensor_message(self, board_index, message):
        p = message.payload
        with self._state_lock:
            if not p.synchronized:
                return None
            generation = self._generations.get(board_index)
            if generation is not None and p.generation != generation:
                if not 0 < (p.generation - generation) & 0xFFFFFFFF < 0x80000000:
                    return None
                self._invalidate_board(board_index)
            self._generations[board_index] = p.generation
            key = board_index, message.antenna_id
            current = p.clock_epoch, p.receiver_epoch, p.generation, p.settings
            previous = self._epochs.get(key)
            if previous is not None:
                if previous[0] != current:
                    self._invalidate_board(board_index)
                elif not 0 < (p.sequence - previous[1]) & 0xFFFFFFFF < 0x80000000:
                    return None
            calibration = self._calibrations.get(board_index)
            if calibration is not None and calibration.epochs[message.antenna_id] != (p.clock_epoch, p.receiver_epoch):
                # apply_config clears packet sequencing, but a preserved
                # calibration still has to witness the same hardware epoch.
                self._invalidate_board(board_index)
            self._epochs[key] = current, p.sequence
            if p.qualified:
                return super()._add_sensor_message(board_index, message)
            return None

    def _on_cluster_updated(self, cache_name, cluster_key, cluster):
        self._try_callbacks(cluster)
        # Retain even delivered events until expiry so late peer captures still
        # see all candidates and cannot contaminate a different repeated PDU.
        return False

    def _get_cluster_cache_timeout(self, cache_name):
        return self.settle_timeout

    def _on_cluster_expired(self, cache_name, cluster_key, cluster):
        cluster.settled = True
        self._try_callbacks(cluster)
        return False

    def _invalidate_board(self, board_index):
        """Caller holds state lock. Do not mix receiver epochs or retain calibration."""
        self._calibrations.pop(board_index, None)
        with self._cluster_lock:
            cache = self._cluster_caches.get("packets", {})
            for key in [k for k, c in cache.items() if c.board_index == board_index]:
                cache.pop(key)

    def modes_changed(self, board_index, before, after):
        if any(old != new and "ble" in (old, new) for old, new in zip(before, after)):
            self.invalidate_calibration(board_index)

    def invalidate_calibration(self, board_index=None):
        """Discard corrections after external mode, tuning or gain changes."""
        with self._state_lock:
            indices = range(len(self.boards)) if board_index is None else [board_index]
            for b in indices:
                self._invalidate_board(b)

    def is_calibrated(self, board_index=0):
        with self._state_lock:
            return board_index in self._calibrations

    def apply_config(self, config, *, board_index=None):
        """Apply board-wide live BLE settings without selecting sensor modes.

        RF input, board-wide gain and capture length/rate preserve the
        reference correction. They do not retune the held oscillator. Capture
        timing is estimated per packet, with rate-aware IF correction. Other
        configuration changes and
        unexpected receiver epochs invalidate it. Direct board calls remain
        available for applications managing their own calibration.
        """
        indices = range(len(self.boards)) if board_index is None else [board_index]
        with self._control_lock:
            for b in indices:
                with self._state_lock:
                    board = self.boards[b]
                    previous = board.ble.get_config()
                    calibration = self._calibrations.get(b)
                    self._invalidate_board(b)
                    state = board.ble.set_config(config)
                    preserved = all(previous.get(k) == state.get(k) for k in board.ble.CONFIG_KEYS
                                    if k not in ("rf_switch", "rx_gain", "gain_mode", "decimation"))
                    if previous.get("gain_mode") != state.get("gain_mode") and "expert" in (previous.get("gain_mode"), state.get("gain_mode")):
                        preserved = False
                    if calibration is not None and preserved:
                        settings = board.ble.packet_settings(state)
                        self._calibrations[b] = replace(calibration, generation=state["generation"], settings=settings)
                    self._generations[b] = state["generation"]
                    self._epochs = {k: v for k, v in self._epochs.items() if k[0] != b}

    def phase(self, cluster, *, calibrated=True):
        """Estimate array-shaped phase, optionally applying a current calibration.

        Raises BLECalibrationError if the event's receiver context does not match
        the stored reference measurement. Raw packet callbacks remain available.
        """
        return self.packet_iq(cluster, calibrated=calibrated).phase()

    def packet_iq(self, cluster, *, calibrated=True):
        """Return aligned full-packet IQ with gain and optional REF correction.

        Integer and fractional capture timing and its IF phase are corrected
        separately for every packet. Packets retain an arbitrary common phase.
        Use spatial covariance to combine them, not coherent sample averaging.
        """
        from .ble_iq import align_packet_iq
        if calibrated and (cluster.packet.settings >> 24) & 0xff == 1:
            if any(not p.gain_reported for a in range(8)
                   if (p := cluster.get_packet(cluster.board_index, a)) is not None):
                raise BLECalibrationError("BLE AGC phase requires packet gain metadata. Update sensor firmware")
        result = align_packet_iq(cluster)
        if not calibrated:
            return result
        with self._state_lock:
            calibration = self._calibrations.get(cluster.board_index)
            if calibration is None or not calibration.matches(cluster):
                raise BLECalibrationError("Calibrate BLE receivers before using corrected IQ")
            return result.corrected(calibration.model)

    def channel_response(self, cluster, *, calibrated=True, correct_timing=True, nfft=128):
        """Relative channel response across the BLE band, not OFDM subcarriers."""
        from .ble_iq import channel_response
        return channel_response(self.packet_iq(cluster, calibrated=calibrated), nfft,
                                correct_timing=correct_timing)

    def calibrate(self, *, board_index=0, samples=8, timeout=8.0, reference_address=None):
        """Measure narrowband phase on REF and restore the previous antenna input.

        Only advertisements from the reference transmitter are accepted. By
        default its address comes from the controller. Supply ``reference_address``
        explicitly when using an external reference generator.

        Requires all eight sensors on this board in BLE mode, LE1M or LE2M,
        and a reference signal on the selected channel. Existing stream processing
        must be running. Acquisition temporarily selects manual gain using the
        configured rx_gain index. The previous gain mode and RF input are restored
        on success and failure. PCB reference propagation delays are
        removed using the board revision. No persistent settings or TX policy change.
        """
        from .ble_calibration import calibrate

        return calibrate(self, board_index, samples, timeout, reference_address)
