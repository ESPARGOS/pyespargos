"""Board-local WiFi IQ measurements and reference-path phase calibration."""

from collections import OrderedDict, deque
from contextlib import ExitStack
from copy import copy
from dataclasses import dataclass, replace
import threading
import time

import numpy as np

from .. import gain_phase_calibration
from . import estimator
from ..clustering import ClusterAssembler
from ..sensor_cluster import SensorCluster
from ..sensor_backlog import BacklogField


@dataclass(frozen=True)
class _Frame:
    board: int
    clock_epoch: int
    frequency: int
    generation: int
    rf_switch: int
    source_mac: str | None
    sequence_control: int
    length: int
    prefix: bytes
    is_11b: bool = False

    @property
    def timing_window_us(self):
        # A management or data header identifies the frame beyond its timestamp.
        # Retries and control frames can repeat within a short interframe space.
        if len(self.prefix) >= 24 and self.prefix[0] & 12 != 4 and not self.prefix[1] & 8:
            return 250
        return 8


# FFT window shifts are integral at 20 MSa/s and periodic over one L-LTF.
_WINDOW_ROTATION = np.exp(-2j * np.pi / 64 * np.outer(np.arange(64), np.arange(-26, 27))).astype(np.complex64)


class _WindowTiming:
    """Relative ADC counter origins for the antennas that observed a common packet."""

    def __init__(self, shape, period=64):
        self.period = period
        self.origins = np.full(shape, -1, dtype=int)
        self.groups = np.full(shape, -1, dtype=int)
        self.epochs = np.zeros(shape, dtype=np.uint32)
        self.next_group = 0
        self.packet_offsets = {}

    def compare_packet(self, first, second, *, learn=False):
        """Compare two training positions in the sensors' 16K-sample ADC rings."""
        if first[0] > second[0]:
            first, second = second, first
        a, epoch_a, index_a = first
        b, epoch_b, index_b = second
        if a == b or index_a < 0 or index_b < 0:
            return None
        key = a, b
        epochs = epoch_a, epoch_b
        offset = (index_a - index_b) % 16384
        previous = self.packet_offsets.get(key)
        agrees = previous is not None and previous[0] == epochs and abs((offset - previous[1] + 8192) % 16384 - 8192) <= 32
        if learn:
            # Two independently identified packets must agree before their
            # relative ADC positions can identify another packet.
            self.packet_offsets[key] = epochs, offset, agrees
        return agrees if previous is not None and previous[0] == epochs and previous[2] else None

    @staticmethod
    def _initial_shift(a, b):
        # Common training locates the two FFT windows before their counters
        # have a shared origin. Only accept a coherent whole-sample match.
        k = np.arange(len(a)) - len(a) // 2
        use = (k != 0) & np.isfinite(a) & np.isfinite(b)
        if not use.any():
            return None
        x, y = a[use], b[use]
        power = np.linalg.norm(x) * np.linalg.norm(y)
        cross = np.zeros(64, dtype=complex)
        cross[k[use] % 64] = x.conj() * y
        lags = np.arange(-16, 17)
        scores = np.abs(np.fft.fft(cross)[lags])
        peak = scores.argmax()
        return int(lags[peak]) if power and scores[peak] >= 0.8 * power else None

    def shifts(self, positions, epochs, channels=None):
        present = positions >= 0
        # An individual sensor can leave WiFi IQ and rejoin while the other
        # ADC counters keep running. Its new ring has an unrelated origin.
        self.groups[present & (self.epochs != epochs)] = -1
        self.epochs[present] = epochs[present]
        shifts = np.zeros(positions.shape, dtype=int)
        if np.count_nonzero(present) < 2:
            return shifts
        known = present & (self.groups >= 0)
        anchor = np.flatnonzero(known if known.any() else present)[0]
        if not known.any():
            self.groups.flat[anchor] = self.next_group
            self.origins.flat[anchor] = 0
            self.next_group += 1
        group = self.groups.flat[anchor]
        packet_time = positions.flat[anchor] - self.origins.flat[anchor]
        spectra = None if channels is None else channels.reshape(positions.size, -1)
        for i in np.flatnonzero(present & (self.groups != group)):
            old = self.groups.flat[i]
            if old == group:
                continue  # Another antenna just connected this group.
            shift = 0 if spectra is None else self._initial_shift(spectra[anchor], spectra[i])
            if shift is None:
                continue
            offset = positions.flat[i] - packet_time - shift
            if old < 0:
                self.origins.flat[i] = offset % self.period
                self.groups.flat[i] = group
            else:
                members = self.groups == old
                self.origins[members] = (self.origins[members] + offset - self.origins.flat[i]) % self.period
                self.groups[members] = group
        linked = present & (self.groups == group)
        shifts[linked] = (positions[linked] - self.origins[linked] - packet_time) % self.period
        return shifts


class WiFiIQCluster(SensorCluster):

    def __init__(self, key, revisions, timing, estimator="training"):
        super().__init__(revisions)
        self.frame, self.training_time = key[:2]
        self.is_11b = self.frame.is_11b
        self.has_lltf = not self.is_11b
        self._barker_reference = None
        self.board_index = self.frame.board
        self.source_mac = self.frame.source_mac
        self.host_timestamp = time.time()
        self.sensor_timestamps = np.full(self.shape, np.nan)
        self.rx_gain = np.full(self.shape, np.nan)
        self.cfo = np.full(self.shape, np.nan)
        self.rf_switch_state = np.full(self.shape, 255, dtype=np.uint8)
        self.epochs = np.zeros(self.shape, np.uint32)
        self.estimator = estimator
        self._packets = {}
        self._spatial = None
        self._refined = None
        self.timing = timing
        self.ring_positions = np.full(self.shape, -1, dtype=int)
        self.window_shifts = np.zeros(self.shape, dtype=int)
        self._channel = np.full((*self.shape, 35 if self.is_11b else 53), np.nan, np.complex64)

    def add_message(self, board_index, message):
        pos = self.get_sensor_position(board_index, message.antenna_id)
        if self.completion[pos]:
            return False
        p = message.payload
        start, cfo, gain, channel = p.training_measurement
        if self.is_11b:
            if self._barker_reference is None:
                self._barker_reference = p.barker
            else:
                alignment = p.barker.align(self._barker_reference)
                if alignment is None:
                    return False
                offset, rotation = alignment
                start += offset
                channel = channel * rotation
        self._channel[pos] = channel
        if p.ring_start is not None:
            self.ring_positions[pos] = (p.ring_start + start) % 16384
        if self.source_mac and self.frame.timing_window_us == 250:
            new = pos, p.receiver_epoch, self.ring_positions[pos]
            for other in self._packets:
                self.timing.compare_packet(new, (other, self.epochs[other], self.ring_positions[other]), learn=True)
        self._packets[pos] = p
        self._spatial = None
        self._refined = None
        self.sensor_timestamps[pos] = (p.time_us + (start - p.end) / 20) * 1e-6
        self.rx_gain[pos] = gain
        self.epochs[pos] = p.receiver_epoch
        self.cfo[pos] = cfo
        self.rf_switch_state[pos] = p.rf_switch
        self._mark_sensor_position_complete(pos)
        return True

    @property
    def iq_packets(self):
        return tuple(self._packets.values())

    @property
    def spatial_csi(self):
        """Per-stream HT or HE channel, with the pool's board, row and column axes."""
        if self._spatial is not None:
            return self._spatial
        first = None
        for pos, packet in self._packets.items():
            result = packet.spatial_csi
            if result is None:
                continue
            if first is None:
                first = result
                channels = np.full((*self.shape, *first.coefficients.shape), np.nan, np.complex64)
                gains = np.full(self.shape, np.nan)
            if (result.phy, result.mcs, result.spatial_streams) != (first.phy, first.mcs, first.spatial_streams) or not np.array_equal(result.frequencies_hz, first.frequencies_hz) or result.coefficients.shape != first.coefficients.shape:
                continue
            rotation = np.exp(-2j * np.pi * result.frequencies_hz * self.window_shifts[pos] / packet.sample_rate)
            channels[pos] = result.coefficients * rotation
            gains[pos] = result.rx_gain
        if first is not None:
            self._spatial = replace(first, coefficients=channels, rx_gain=gains)
        return self._spatial

    def deserialize_csi_dsss(self):
        return self._channel * _WINDOW_ROTATION[self.window_shifts % 64, 9:-9]

    def deserialize_csi_lltf(self):
        if self.estimator != "training" and self._packets and self._refined is None:
            self._refined = estimator.refine_channels(self._packets, self.estimator, self._channel)
        channels = self._channel if self._refined is None else self._refined
        return channels * _WINDOW_ROTATION[self.window_shifts % 64]


class _PhaseCorrection:
    def __init__(self, cluster, weights, *, epochs=None, gains=None):
        self.clock_epoch = cluster.frame.clock_epoch
        self.frequency = cluster.frame.frequency
        self.epochs = (cluster.epochs if epochs is None else epochs).copy()
        self.gains = (cluster.rx_gain if gains is None else gains).copy()
        self.timing = cluster.timing
        self.weights = weights
        # Factor out the reference gain correction so each packet can use its measured gain.
        self.gain_adjusted_weights = gain_phase_calibration.apply(weights.conj(), self.gains).conj()
        self.generation = None
        self.epochs_valid = True

    def for_cluster(self, cluster):
        frame = cluster.frame
        if cluster.timing is not self.timing or frame.rf_switch == 1 or (frame.clock_epoch, frame.frequency, frame.generation) != (self.clock_epoch, self.frequency, self.generation):
            return None
        usable = cluster.completion & (self.epochs == cluster.epochs) & np.isfinite(cluster.rx_gain)
        if not usable.any():
            return None
        if np.array_equal(usable, cluster.completion) and np.array_equal(self.gains[usable], cluster.rx_gain[usable]):
            return self
        correction = copy(self)
        weights = np.where(usable[..., None], self.gain_adjusted_weights, np.nan)
        correction.weights = gain_phase_calibration.apply(weights, cluster.rx_gain)
        return correction

    def apply_spatial(self, measurement):
        frequencies = measurement.frequencies_hz
        reference = np.arange(-26, 27) * 312500
        weights = np.full((*self.weights.shape[:-1], len(frequencies)), np.nan, np.complex64)
        # AGC can change during HE-STF, after the common preamble.
        valid = np.isfinite(measurement.rx_gain)[..., None] & np.isfinite(self.weights)
        raw = np.where(valid, self.gain_adjusted_weights, np.nan)
        trained = gain_phase_calibration.apply(raw, measurement.rx_gain)
        for pos in np.ndindex(trained.shape[:-1]):
            row = trained[pos]
            if np.isfinite(row).all():
                phase = np.interp(frequencies, reference, np.unwrap(np.angle(row)), left=np.nan, right=np.nan)
                weights[pos] = np.exp(1j * phase)
        return replace(measurement, coefficients=measurement.coefficients * weights[..., None, :])

    def apply_dsss(self, csi):
        return csi * self.weights[..., 9:-9]

    def apply_lltf(self, csi):
        return csi * self.weights


class WiFiIQReceiver(ClusterAssembler):
    """Associate identified WiFi packets by transmitter, frame and training time."""

    backlog_fields = ("lltf", "dsss", "rx_gain", "cfo", "rf_switch_state", "timestamp", "host_timestamp", "mac")

    extra_backlog_fields = {
        "spatial_csi": BacklogField((), object, per_sensor=False, fill_value=None),
        "iq_packets": BacklogField((), object, per_sensor=False, fill_value=None),
        "dsss": BacklogField((35,), np.complex64, per_sensor=True, fill_value=np.nan),
    }

    def calibration_for(self, cluster):
        correction = self._corrections.get(cluster.board_index)
        return correction.for_cluster(cluster) if correction is not None else None

    def __init__(self, owner):
        super().__init__(owner)
        self._estimator = "training"
        self._expected = np.ones(self.shape, bool)
        self._corrections = {}
        self._applied_settings = {}
        self._settings_lock = threading.Lock()
        self._shared = OrderedDict()
        self._window_timing = OrderedDict()
        self._calibration_lock = threading.Lock()
        self._calibration_reference = None
        try:
            for i, board in enumerate(self.boards):
                self._subscribe_sensor_messages(i, board.wifi_iq, board.wifi_iq.subscribe_packets)
        except Exception:
            self._close()
            raise

    def _complete(self, cluster):
        expected = self._expected[cluster.board_index]
        return expected.any() and cluster.completion[cluster.board_index][expected].all()

    def add_callback(self, callback, callback_predicate=None):
        return self.add_cluster_callback(callback, callback_predicate or self._complete)

    def replace_callback(self, handle, callback, callback_predicate=None):
        return self.replace_cluster_callback(handle, callback, callback_predicate or self._complete)

    remove_callback = ClusterAssembler.remove_cluster_callback

    def _get_cluster_cache_name(self, board_index, message):
        return "packets"

    def _superseded(self, board_index, clock_epoch, generation):
        state = self._applied_settings.get(board_index)
        if state is None:
            return False
        current_clock, current_generation, previous_clock = state
        return clock_epoch == previous_clock or (clock_epoch == current_clock and (generation - current_generation) & 0xFFFFFFFF >= 0x80000000)

    def _observe_settings(self, board_index, clock_epoch, generation):
        # Settings replies and stream records can arrive on different threads.
        # Keep the preceding boot so a delayed datagram cannot switch us back.
        with self._settings_lock:
            if self._superseded(board_index, clock_epoch, generation):
                return False
            clock, current, previous = self._applied_settings.get(board_index, (None, None, None))
            if (clock_epoch, generation) != (clock, current):
                if clock_epoch != clock:
                    previous = clock
                self._applied_settings[board_index] = clock_epoch, generation, previous
            return True

    def _apply_config(self, board_index, settings):
        state = self.boards[board_index].wifi_iq.set_config(settings)
        if state.get("clock_epoch") is not None:
            self._observe_settings(board_index, state["clock_epoch"], state["generation"])
        return state

    def _add_sensor_message(self, board_index, message):
        # Some firmware records split one sensor's snapshot and modem verdict.
        # Match them on that sensor's shared trigger before searching for training.
        p = message.payload
        if p.flags & 2 and not self._observe_settings(board_index, p.clock_epoch, p.generation):
            return None
        if p.kind == b"WFM1" or p.kind.startswith(b"WFC"):
            key = board_index, message.antenna_id, p.clock_epoch, p.receiver_epoch, p.generation, p.boot_us
            other = self._shared.pop(key, None)
            if other is None or (other.kind == b"WFM1") == (p.kind == b"WFM1"):
                self._shared[key] = p
                if len(self._shared) > 128:
                    self._shared.popitem(last=False)
            else:
                raw, verdict = (other, p) if p.kind == b"WFM1" else (p, other)
                p = copy(raw)
                shift = p.end - verdict.end + 20 * (verdict.local_end_us - p.local_end_us)
                p.training = max(0, verdict.training + shift) if verdict.training != 65535 else 0
                p.frame = max(0, min(len(p.raw), verdict.frame + shift))
                p.phy, p.evidence, p.length = verdict.phy, verdict.evidence, verdict.length
                p.half_rate = verdict.half_rate
                p.source_mac, p.sequence_control = verdict.source_mac, verdict.sequence_control
                p.prefix, p.crc_valid = verdict.prefix, verdict.crc_valid
                message = replace(message, payload=p)
            if p.kind.startswith(b"WFC") and not p.evidence & 1:
                message = replace(message, payload=copy(p))
        return super()._add_sensor_message(board_index, message)

    def _get_cluster_key(self, board_index, message):
        p = message.payload
        if not p.flags & 2 or not p.raw.size:
            return None
        if p.kind.startswith(b"WFC") and not p.evidence & 1:
            from ..packet_decoders import wifi

            decoded = wifi(p.raw.astype("=u4", copy=False).tobytes())
            if decoded and decoded["mac_valid"] and decoded["crc_valid"] and len(decoded["prefix"]) >= 24:
                p.source_mac = decoded["transmitter"].hex()
                p.prefix = decoded["prefix"]
                p.sequence_control = int.from_bytes(p.prefix[22:24], "little")
                p.training, p.training_words = decoded["training_start"], decoded["training_words"]
                p.cfo, p.phy, p.length = decoded["cfo_hz"], decoded["phy"], decoded["length_bytes"]
                p.half_rate = decoded["rate_half_mbps"]
                p.crc_valid = True
        identified = p.source_mac and p.crc_valid
        if not identified:
            # A single modem cannot decode a spatially multiplexed MAC header.
            # Its independently CRC-checked PHY header still identifies training.
            header = p.phy_header
            if header is None:
                return None
            paths = header["mcs"] // 8 + 1 + header["stbc"] if header["phy"] == 2 else header["he"]["space_time_streams"]
            if paths < 2 or (p.training_words and abs(header["training_start"] - p.training) > 4):
                return None
            if not p.training_words:
                p.training, p.training_words = header["training_start"], header["training_words"]
                p.cfo = header["cfo_hz"]
            p.phy, p.length = header["phy"], header["length_bytes"]
        if p.training_measurement is None:
            return None
        instant = p.time_us + (p.training_measurement[0] - p.end) / 20
        frame = _Frame(
            board_index,
            p.clock_epoch,
            p.frequency_hz,
            p.generation,
            # R and L are simultaneous observations of the same over-air frame.
            2 if p.rf_switch in (2, 3) else p.rf_switch,
            p.source_mac if identified else None,
            p.sequence_control if identified else 0,
            p.length,
            p.prefix if identified else header["prefix"],
            p.phy == 1,
        )
        boot = p.boot_us if p.evidence & (1 << 18) else None
        return frame, instant, boot, message.antenna_id

    def _resolve_cluster_key(self, name, key, cache, board_index, message):
        frame, instant, boot, _ = key

        def same_packet(candidate):
            other, when, trigger, _ = candidate
            if other != frame:
                return False
            if frame.rf_switch == 1 and self._calibration_reference == (frame.board, frame.generation, frame.source_mac):
                # The cabled generator increments the sequence number every
                # transmission. Its identical CRC-checked MAC headers identify
                # one packet even while the sensors' UART clocks are settling.
                return abs(when - instant) <= 5000
            if frame.source_mac is None and boot is not None and trigger is not None:
                # The UART clock offset cancels when comparing the arrival
                # time to the shared GPIO edge on each sensor. A capture can
                # contain two successive packets with identical PHY headers.
                return abs(boot - trigger) <= 500 and abs((instant - boot) - (when - trigger)) <= 3
            if frame.source_mac is None and abs(when - instant) <= 250:
                cluster = cache[candidate]
                p = message.payload
                if p.ring_start is not None:
                    pos = cluster.get_sensor_position(board_index, message.antenna_id)
                    index = (p.ring_start + p.training_measurement[0]) % 16384
                    comparisons = [cluster.timing.compare_packet((pos, p.receiver_epoch, index), (q, cluster.epochs[q], cluster.ring_positions[q])) for q in cluster._packets]
                    known = [v for v in comparisons if v is not None]
                    if known:
                        return all(known)
            # A UART timestamp alone cannot identify a spatially multiplexed frame.
            return frame.source_mac is not None and abs(when - instant) <= frame.timing_window_us

        matches = [k for k in cache if same_packet(k)]
        if len(matches) > 1:
            return None
        return matches[0] if matches else key

    def _create_cluster(self, name, key, board_index, message):
        frame = key[0]
        correction = self._corrections.get(board_index)
        if correction is not None and (frame.generation, frame.clock_epoch, frame.frequency) == (correction.generation, correction.clock_epoch, correction.frequency):
            timing = correction.timing
        else:
            session = board_index, frame.generation, frame.clock_epoch, frame.frequency
            if session not in self._window_timing:
                self._window_timing[session] = _WindowTiming(self.shape, period=256)
                if len(self._window_timing) > 4 * len(self.boards):
                    self._window_timing.popitem(last=False)
            timing = self._window_timing[session]
        return WiFiIQCluster(key, self.board_revisions, timing, "training" if frame.rf_switch == 1 else self.estimator)

    def _on_cluster_updated(self, name, key, cluster):
        if self._superseded(cluster.board_index, cluster.frame.clock_epoch, cluster.frame.generation):
            return True
        correction = self._corrections.get(cluster.board_index)
        if correction is not None and correction.timing is cluster.timing:
            if np.any(cluster.completion & np.isfinite(correction.gains) & (cluster.epochs != correction.epochs)):
                correction.epochs_valid = False
        cluster.window_shifts = cluster.timing.shifts(cluster.ring_positions, cluster.epochs, cluster._channel)
        self._try_callbacks(cluster)
        return False

    def _on_cluster_expired(self, name, key, cluster):
        if not self._superseded(cluster.board_index, cluster.frame.clock_epoch, cluster.frame.generation):
            self._try_callbacks(cluster)
        return False

    def _get_cluster_cache_timeout(self, name):
        return 0.5

    def modes_changed(self, board_index, before, after):
        if any(a != b and "wifi_iq" in (a, b) for a, b in zip(before, after)):
            self._corrections.pop(board_index, None)
            self._clear_cluster_cache("packets")

    @property
    def estimator(self):
        """Waveform fields used for CSI: training, headers, or payload."""
        return self._estimator

    @estimator.setter
    def estimator(self, mode):
        if mode not in estimator.MODES:
            raise ValueError(f"Unknown WiFi IQ estimator: {mode}")
        self._estimator = mode
        self._clear_cluster_cache("packets")

    def has_calibration(self, board_index=0):
        correction = self._corrections.get(board_index)
        if correction is None or not correction.epochs_valid:
            return False
        state = self._applied_settings.get(board_index)
        return state is None or state[:2] == (correction.clock_epoch, correction.generation)

    def configure(self, settings, board_index=0):
        """Apply radio settings and retain a compatible phase reference."""
        radio = self.boards[board_index].wifi_iq
        with self._calibration_lock:
            before = radio.get_config()
            after = self._apply_config(board_index, settings)
            correction = self._corrections.get(board_index)
            if correction is not None:
                same_channel = before.get("channel") == after.get("channel")
                same_gain = all(before.get(k) == after.get(k) for k in ("gain_mode", "rx_gain", "expert_gain_words"))
                compatible_gain = same_gain or (after["gain_mode"] in ("manual", "auto") and before["gain_mode"] != "expert")
                if correction.generation == before["generation"] and same_channel and compatible_gain:
                    updated = copy(correction)
                    updated.generation = after["generation"]
                    self._corrections[board_index] = updated
                else:
                    self._corrections.pop(board_index, None)
            return after

    def calibrate(self, board_index=0, timeout=10):
        """Measure relative L-LTF phase using the dedicated or local WiFi IQ reference generator."""
        if not np.isfinite(timeout) or timeout <= 0:
            raise ValueError("Calibration timeout must be positive")
        with self._calibration_lock:
            with ExitStack() as restore:
                correction = self._measure_reference(board_index, timeout, restore)
            self._corrections[board_index] = correction

    def _measure_reference(self, board_index, timeout, restore):
        board = self.boards[board_index]
        config = board.wifi_iq.get_config()
        expected = self._expected[board_index].copy()
        if config["gain_mode"] not in ("manual", "auto") or not expected.any():
            raise ValueError("WiFi IQ calibration requires active receivers in automatic or manual gain mode")
        address = config["reference_address"].replace(":", "").lower()
        self._corrections.pop(board_index, None)
        done = threading.Event()
        antennas = list(zip(*np.where(expected)))
        anchor = (board_index, *antennas[0])
        observations = [deque(maxlen=8) for _ in antennas]
        epochs = np.zeros(self.shape, dtype=np.uint32)
        gains = np.full(self.shape, np.nan)
        reference_timing = None
        result = []
        controller = getattr(self._owner, "reference_controller", None)
        if controller is not None:
            from ..controller_reference import prepare_packet_reference

            address = prepare_packet_reference(restore, controller, "wifi_iq", config["channel"]).replace(":", "").lower()
        else:
            from ..controller_reference import select_reference_mode

            select_reference_mode(restore, board, "wifi_iq")

        # The cabled reference has its own signal level. Use a repeatable gain
        # for the measurement, regardless of how the antennas receive packets.
        # A short OFDM capture is sufficient for the reference packet. An
        # antenna's longer capture or low reporting limit can otherwise leave
        # the receivers reporting different packets from the same generator.
        reference_config = dict(rf_switch=1, gain_mode="manual", rx_gain=30, boot_sync=False, ofdm_words=1024, dsss_words=1024, max_pps=250)
        antenna_config = {key: config[key] for key in reference_config}

        def restore_input():
            generation = self._apply_config(board_index, antenna_config)["generation"]
            if result:
                result[0].generation = generation

        restore.callback(restore_input)
        reference = self._apply_config(board_index, reference_config)
        self._calibration_reference = board_index, reference["generation"], address
        restore.callback(setattr, self, "_calibration_reference", None)

        def observe(cluster):
            nonlocal reference_timing
            if done.is_set() or not cluster.has_lltf or cluster.frame.generation != reference["generation"]:
                return
            if cluster.board_index != board_index or cluster.source_mac != address:
                return
            valid = cluster.completion & (cluster.rf_switch_state == 1) & np.isfinite(cluster.rx_gain)
            if not valid[anchor]:
                return
            # All phases are relative to the same antenna, but different
            # pairs can hear different transmissions of the reference packet.
            if reference_timing is not cluster.timing or epochs[anchor] != cluster.epochs[anchor] or gains[anchor] != cluster.rx_gain[anchor]:
                for history in observations:
                    history.clear()
                reference_timing = cluster.timing
            channels = cluster.deserialize_csi_lltf()
            for antenna, history in zip(antennas, observations):
                pos = (board_index, *antenna)
                if not valid[pos]:
                    continue
                if epochs[pos] != cluster.epochs[pos] or gains[pos] != cluster.rx_gain[pos]:
                    history.clear()
                epochs[pos], gains[pos] = cluster.epochs[pos], cluster.rx_gain[pos]
                relative = channels[pos] * channels[anchor].conj()
                history.append(relative / np.maximum(np.abs(relative), 1e-12))
            if any(len(history) < 4 for history in observations):
                return
            mean = np.array([np.mean(history, axis=0) for history in observations])
            if not np.all(np.median(np.abs(mean), axis=-1) >= 0.9):
                return
            freq = cluster.frame.frequency + np.arange(-26, 27) * 312500
            delays = board.revision.calib_trace_delays
            trace = np.exp(-2j * np.pi * (delays - delays[expected][0])[..., None] * freq)
            weights = np.full((*self.shape, 53), np.nan, np.complex64)
            weights[board_index][expected] = np.exp(-1j * np.angle(mean)) * trace[expected]
            result.append(_PhaseCorrection(cluster, weights, epochs=epochs, gains=gains))
            done.set()

        handle = self.add_callback(observe, lambda c: self._complete(c) or c.age >= 0.05)
        restore.callback(self.remove_callback, handle)
        deadline = time.monotonic() + timeout
        while not done.is_set():
            if time.monotonic() >= deadline:
                raise TimeoutError("No coherent WiFi IQ reference. Check reference generation, gain and signal level.")
            if self.processing:
                done.wait(0.05)
            else:
                self._owner.run(timeout=0.05)
        return result[0]
