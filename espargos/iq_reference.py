"""Broadband reference acquisition shared by IQ synchronization and calibration."""

import threading
import time
from contextlib import contextmanager

import numpy as np

from .iq_packet import DECIM_TO_FS, IQChunkPacket, IQ_CHUNK_TYPE_HEADER, IQ_CHUNK_FLAG_REFERENCE, IQ_CHUNK_FLAG_REFERENCE_READY

REFERENCE = IQ_CHUNK_FLAG_REFERENCE
READY = IQ_CHUNK_FLAG_REFERENCE_READY


def transfer_delay(timing_seconds, old_origin_ns, new_origin_ns, reference):
    """Transfer a full-rate delay fit to a new, timestamped sample grid."""
    delta = (np.asarray(new_origin_ns, dtype=np.int64) - np.asarray(old_origin_ns, dtype=np.int64)).reshape(-1)
    delta -= delta[reference]
    # All supported rates have wrap periods that are multiples of the
    # 24-bit chunk-index wrap at 80 MS/s. Remove only those whole wraps.
    wrap_ns = 53687091200
    delta = (delta + wrap_ns // 2) % wrap_ns - wrap_ns // 2
    return np.asarray(timing_seconds) + delta * 1e-9


def relative_lag(samples, reference, expected_lag=None):
    """Sample in ``samples`` coincident with reference[0], plus confidence."""
    x = np.asarray(samples) - np.mean(samples)
    y = np.asarray(reference) - np.mean(reference)
    n = len(y)
    lags = np.arange(-n + 1, len(x))
    cross = np.correlate(x, y, mode="full")
    power = np.correlate(np.abs(x) ** 2, np.ones(n), mode="full")
    refpower = np.correlate(np.ones(len(x)), np.abs(y) ** 2, mode="full")
    score = np.abs(cross) / np.sqrt(np.maximum(power * refpower, 1e-20))
    score[np.abs(lags) > n - 256] = 0
    if expected_lag is not None:
        # Receiver phase and the startup envelope move the threshold crossing
        # by tens of samples. Let the full waveform locate the peak; use the
        # edge only to bound the search and break truly periodic ties.
        score[np.abs(lags - expected_lag) > 96] = 0
    best = np.argmax(score)
    if expected_lag is not None:
        ties = np.flatnonzero(score >= score[best] - 1e-5)
        best = ties[np.argmin(np.abs(lags[ties] - expected_lag))]
    if score[best] < 0.65:
        raise RuntimeError(f"reference snapshots do not overlap coherently (correlation {score[best]:.3f})")
    return int(lags[best]), float(score[best])


@contextmanager
def reference_session(pool, center_hz=None, rx_gain=35):
    if not pool._reference_lock.acquire(blocking=False):
        raise RuntimeError("Synchronization or calibration is already running")
    try:
        with _reference_session(pool, center_hz, rx_gain) as state:
            yield state
    finally:
        pool._reference_lock.release()


@contextmanager
def _reference_session(pool, center_hz=None, rx_gain=35):
    """Restore each board's settings on every exit, including failed acquisition."""
    saved = [b.iq.get_config() for b in pool.boards]
    center = int(center_hz or saved[0]["receivers"][0]["rf_freq_hz"])
    fs = DECIM_TO_FS[saved[0]["adc_decimation"]]
    if not 2310e6 <= center <= 2640e6:
        raise ValueError("Broadband reference requires a center frequency from 2310 to 2640 MHz")
    if any(c["adc_decimation"] != saved[0]["adc_decimation"] for c in saved):
        raise ValueError("All boards must use the same IQ sample rate")
    if any(any(r["rf_freq_hz"] != center for r in c["receivers"]) for c in saved) and center_hz is None:
        raise ValueError("All receivers must use the same center frequency")
    pool.start_processing()
    pool._calibrating = True
    try:
        pool.master_board.iq.set_reference()
        pool.apply_config({"mode": "iq", "adc_decimation": 1, "rf_switch": 1, "gain_mode": "manual", "rx_gain": rx_gain, "rf_freq_hz": center, "trigger": {"mode": "interval", "interval_chunks": 2048, "burst_chunks": 4}})
        yield center, fs
    finally:
        try:
            pool.master_board.iq.set_reference()
        finally:
            errors = []
            for board, config in zip(pool.boards, saved):
                restore = {k: config[k] for k in ("rf_switch", "trigger", "receivers", "adc_decimation")}
                # An explicitly requested calibration retune becomes the new operating frequency.
                if center_hz is not None:
                    restore["receivers"] = [dict(r, rf_freq_hz=center) for r in config["receivers"]]
                try:
                    board.iq.set_config(restore)
                except Exception as error:
                    errors.append(error)
            pool._calibrating = False
            if errors:
                raise RuntimeError(f"Failed to restore receive settings: {errors}")


def acquire_grid(pool, center, timeout=3.0):
    """Capture one common RF onset without prior synchronization or Wi-Fi."""
    count = len(pool.boards)
    ready, snapshots, generations = {}, {}, {}
    changed = threading.Condition()
    subscriptions = []

    def receive(board_index, message):
        try:
            p = IQChunkPacket(message.payload)
        except ValueError:
            return
        if not p.flags & (REFERENCE | READY):
            return
        key = (board_index, message.antenna_id)
        with changed:
            if p.flags & READY:
                ready[key] = p.config_generation
            else:
                snapshots.setdefault((key, p.config_generation), {})[p.capture_chunk_offset] = p
            changed.notify_all()

    def wait(predicate, detail):
        deadline = time.monotonic() + timeout
        with changed:
            while not predicate():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeError(f"Broadband sync timed out waiting for {detail}; ready={len(ready)}/{8*count}, snapshots={[(k, len(v)) for k,v in snapshots.items()]}")
                changed.wait(left)

    try:
        for bi, board in enumerate(pool.boards):
            subscriptions.append(board.subscribe_sensor_messages(IQ_CHUNK_TYPE_HEADER, lambda m, bi=bi: receive(bi, m)))
        pool.master_board.iq.set_reference("iq", center, 30)
        pool.master_board.control.command("gate_iq_reference", {"open": False})
        pool.apply_config({"adc_decimation": 1, "trigger": {"mode": "reference", "threshold": 32}})
        for bi, board in enumerate(pool.boards):
            generations[bi] = board.iq.get_config()["config_generation"]
        wait(lambda: all(ready.get((bi, a)) == generations[bi] for bi in range(count) for a in range(8)), "quiet REF readiness")
        pool.master_board.control.command("gate_iq_reference", {"open": True})
        wait(lambda: all(len(snapshots.get(((bi, a), generations[bi]), {})) == 4 for bi in range(count) for a in range(8)), "reference onset snapshots")
        ref_packets = snapshots[((0, 0), generations[0])]
        reference = np.concatenate([ref_packets[k].decode_iq() for k in range(4)])
        ref_edge = ref_packets[0].producer_wake_write_ptr
        anchors, scores = [], []
        for bi in range(count):
            values = []
            for a in range(8):
                packets = snapshots[((bi, a), generations[bi])]
                samples = np.concatenate([packets[k].decode_iq() for k in range(4)])
                lag, score = relative_lag(samples, reference, packets[0].producer_wake_write_ptr - ref_edge)
                values.append(packets[0].fire_time_ns + round(lag * 1e9 / 80e6))
                scores.append(score)
            anchors.append(values)
        # One sequence names this grid across every board in the pool.
        pool._reference_sequence = (pool._reference_sequence + 1) & 0xFFFFFFFF or 1
        for board, values in zip(pool.boards, anchors):
            board.iq.set_sync_anchor(values, seq=pool._reference_sequence)
        pool.apply_config({"trigger": {"mode": "interval", "interval_chunks": 4096, "burst_chunks": 4}})
        pool.clear_chunks()
        pool._logger.info("Broadband onset correlation min %.3f", min(scores))
        return [pool._reference_sequence] * count
    finally:
        # Restore reference routing even when acquisition or transport fails.
        try:
            pool.master_board.control.command("gate_iq_reference", {"open": True})
        finally:
            for board, handle in zip(pool.boards, subscriptions):
                board.unsubscribe_sensor_messages(handle)


def collect(pool, count=16, timeout=3.0, seqs=None):
    """Wait on useful data, not a fixed dwell or diagnostic polling loop."""
    clusters = []
    event = threading.Event()
    generations = np.array([b.iq.get_config()["config_generation"] for b in pool.boards])[:, None, None]

    def cb(c):
        if event.is_set():
            return
        if not np.all(c.config_generation == generations):
            return
        # Discard quiet/startup data already in transport when TX became ready.
        ac = c.iq - np.mean(c.iq, axis=-1, keepdims=True)
        if np.min(np.mean(np.abs(ac) ** 2, axis=-1)) < 100:
            return
        if np.max(np.abs(c.iq.real)) >= 510 or np.max(np.abs(c.iq.imag)) >= 510:
            return
        if not np.all(c.sync_info & (1 << 16)):
            return
        if seqs is not None and not np.all((c.sync_info & 0xFFFF) == (np.asarray(seqs)[:, None, None] & 0xFFFF)):
            return
        if clusters and not np.array_equal(c.fire_time_ns, clusters[0].fire_time_ns):
            clusters.clear()
        clusters.append(c)
        if len(clusters) >= count:
            event.set()

    handle = pool.add_iq_callback(cb)
    try:
        if not event.wait(timeout):
            raise RuntimeError(f"Only {len(clusters)}/{count} synchronized broadband snapshots received")
        return clusters[:count]
    finally:
        pool.remove_iq_callback(handle)


def fit(clusters, ref_flat):
    """Coherence-selected cross-spectral phase and delay, in sample units."""
    x = np.stack([c.iq.reshape(-1, 256) for c in clusters])
    x -= np.mean(x, axis=-1, keepdims=True)
    F = np.fft.fftshift(np.fft.fft(x * np.hanning(256), axis=-1), axes=-1)
    cross = np.mean(F * np.conj(F[:, ref_flat : ref_flat + 1]), axis=0)
    power = np.mean(np.abs(F) ** 2, axis=0)
    coherence = np.abs(cross) ** 2 / np.maximum(power * power[ref_flat], 1e-20)
    f = np.fft.fftshift(np.fft.fftfreq(256))
    taus, phases, qualities = [], [], []
    for a, z in enumerate(cross):
        valid = (coherence[a] > 0.75) & (power[a] > np.max(power[a]) * 0.01) & (np.abs(f) > 2 / 256)
        if np.count_nonzero(valid) < 8 or np.ptp(f[valid]) < 0.08:
            raise RuntimeError(f"Sensor {a}: insufficient coherent reference bandwidth")
        zz, ff = z[valid], f[valid]
        # Estimate the slope from the closest coherent frequency pairs, then
        # remove it before unwrapping across gaps (including excluded DC).
        spacing = np.diff(ff)
        nearest = spacing == spacing.min()
        tau0 = np.angle(np.sum(zz[1:][nearest] * np.conj(zz[:-1][nearest]))) / (2 * np.pi * spacing.min())
        phase = np.unwrap(np.angle(zz * np.exp(-2j * np.pi * ff * tau0)))
        slope, _ = np.polyfit(ff, phase, 1, w=np.sqrt(np.abs(zz)))
        tau = tau0 + slope / (2 * np.pi)
        response = np.sum(zz * np.exp(-2j * np.pi * ff * tau))
        quality = abs(response) / np.sum(np.abs(zz))
        if quality < 0.95 or abs(tau) > 31.5:
            raise RuntimeError(f"Sensor {a}: reference delay fit failed (delay {tau:.2f}, coherence {quality:.3f})")
        taus.append(tau)
        phases.append(np.angle(response))
        qualities.append(quality)
    return np.array(taus), np.array(phases), np.array(qualities)
