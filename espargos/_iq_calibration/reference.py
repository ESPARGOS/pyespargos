"""Broadband reference acquisition shared by IQ synchronization and calibration."""

import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ..iq_packet import DECIM_TO_FS, IQChunkPacket, IQ_CHUNK_TYPE_HEADER, IQ_CHUNK_FLAG_REFERENCE, IQ_CHUNK_FLAG_REFERENCE_READY

REFERENCE = IQ_CHUNK_FLAG_REFERENCE
READY = IQ_CHUNK_FLAG_REFERENCE_READY

# Reduce antenna leakage, using a stronger wired reference for a clear onset.
REFERENCE_RX_GAIN = 30
REFERENCE_EXTERNAL_RX_GAIN = 42
REFERENCE_THRESHOLD = 48
REFERENCE_BACKOFF = 0


def _map(function, values):
    """Independent controller operations, joined before returning or raising."""
    values = list(values)
    with ThreadPoolExecutor(max_workers=max(1, len(values))) as executor:
        return list(executor.map(function, values))


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
        # Try near the measured edge first. The edge is relative to a chunk,
        # so receivers choosing different onset chunks need the full search.
        nearby = np.where(np.abs(lags - expected_lag) <= 96, score, 0)
        if np.max(nearby) >= 0.65:
            score = nearby
    best = np.argmax(score)
    if expected_lag is not None:
        ties = np.flatnonzero(score >= score[best] - 1e-5)
        best = ties[np.argmin(np.abs(lags[ties] - expected_lag))]
    if score[best] < 0.65:
        raise RuntimeError(f"reference snapshots do not overlap coherently (correlation {score[best]:.3f})")
    return int(lags[best]), float(score[best])


@contextmanager
def reference_session(pool, center_hz=None, rx_gain=None, scope=None):
    if not pool._reference_lock.acquire(blocking=False):
        raise RuntimeError("Synchronization or calibration is already running")
    try:
        with _reference_session(pool, center_hz, rx_gain, scope) as state:
            yield state
    finally:
        pool._reference_lock.release()


@contextmanager
def _reference_session(pool, center_hz=None, rx_gain=None, scope=None):
    """Restore each board's settings on every exit, including failed acquisition."""
    from .scope import CalibrationScope

    scope = scope or CalibrationScope(pool)
    saved = _map(lambda b: b.iq.get_config(), pool.boards)
    bi, a = scope.pairs[0]
    center = int(center_hz or saved[bi]["receivers"][a]["rf_freq_hz"])
    fs = DECIM_TO_FS[saved[bi]["adc_decimation"]]
    if not 2310e6 <= center <= 2640e6:
        raise ValueError("Broadband reference requires a center frequency from 2310 to 2640 MHz")
    if any(DECIM_TO_FS[saved[bi]["adc_decimation"]] != fs for bi in scope.active):
        raise ValueError("All boards must use the same IQ sample rate")
    if any(saved[bi]["receivers"][a]["rf_freq_hz"] != center for bi, a in scope.pairs) and center_hz is None:
        raise ValueError("All receivers must use the same center frequency")
    if rx_gain is None:
        # The external splitter/cables attenuate REF before it reaches the
        # sensors. Keep the lower gain for a board's direct internal path.
        external = pool.master_board.wifi_rx.get_config()["calib-source"] != 0
        rx_gain = REFERENCE_EXTERNAL_RX_GAIN if external else REFERENCE_RX_GAIN
    from ..iq_tx_control import _rpc, _wait

    muted = {bi: _rpc(pool.boards[bi], "get_iq_tx")["muted"] for bi in scope.active}
    pool._calibrating = True
    try:
        for bi in scope.active:
            _rpc(pool.boards[bi], "set_iq_tx", {"muted": True})
            _wait(pool.boards[bi], lambda state: all(not x["enabled"] or abs(x["applied_amplitude"]) < 1e-8 for x in state["sensors"]), 3)
        pool.master_board.iq.set_reference()
        scope.apply_rx({"mode": "iq", "adc_decimation": 1, "rf_switch": 1, "gain_mode": "manual", "rx_gain": rx_gain, "rf_freq_hz": center, "trigger": {"mode": "interval", "interval_chunks": 16384, "burst_chunks": 1}})
        _map(lambda bi: _wait(pool.boards[bi], lambda state: all(state["sensors"][a]["applied_mode"] == "iq_rx" for a in scope.ids[bi]), 3), scope.active)
        yield center, fs
    finally:
        try:
            pool.master_board.iq.set_reference()
        finally:
            errors = []

            def restore_board(pair):
                bi, config = pair
                board = pool.boards[bi]
                restore = {k: config[k] for k in ("rf_switch", "trigger", "adc_decimation")}
                restore["receivers"] = [config["receivers"][a] for a in scope.ids[bi]]
                # An explicitly requested calibration retune becomes the new operating frequency.
                if center_hz is not None:
                    restore["receivers"] = [dict(r, rf_freq_hz=center) for r in restore["receivers"]]
                try:
                    board.iq.set_config(restore)
                except Exception as error:
                    errors.append(error)

            _map(restore_board, [(bi, saved[bi]) for bi in scope.active])
            pool._calibrating = False
            if errors:
                raise RuntimeError(f"Failed to restore receive settings: {errors}")
            for bi, was_muted in muted.items():
                _rpc(pool.boards[bi], "set_iq_tx", {"muted": was_muted})


def acquire_grid(pool, center, timeout=3.0, scope=None):
    """Capture one common RF onset without prior synchronization or Wi-Fi."""
    from .scope import CalibrationScope

    scope = scope or CalibrationScope(pool)
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
                    missing = [(bi, a) for bi, a in scope.pairs if ready.get((bi, a)) != generations.get(bi)]
                    captured = [(bi, a, generation) for (bi, a), generation in snapshots]
                    pool._logger.debug("REF timeout: generations=%s, ready=%s, captured=%s", generations, ready, captured)
                    if not missing:
                        missing = [(bi, a) for bi, a in scope.pairs if len(snapshots.get(((bi, a), generations.get(bi)), {})) != 4]
                    antennas = ", ".join(f"{pool.boards[bi].host}/{a}" for bi, a in missing)
                    raise RuntimeError(f"Broadband sync timed out waiting for {detail}: {antennas}")
                changed.wait(left)

    try:
        for bi in scope.active:
            board = pool.boards[bi]
            subscriptions.append((board, board.subscribe_sensor_messages(IQ_CHUNK_TYPE_HEADER, lambda m, bi=bi: receive(bi, m))))
        pool.master_board.iq.set_reference("iq", center, REFERENCE_BACKOFF)
        pool.master_board.control.command("gate_iq_reference", {"open": False})
        scope.apply_rx({"adc_decimation": 1, "trigger": {"mode": "reference", "threshold": REFERENCE_THRESHOLD}})
        generations = dict(enumerate(_map(lambda b: b.iq.get_config()["config_generation"], pool.boards)))
        wait(lambda: all(ready.get((bi, a)) == generations[bi] for bi, a in scope.pairs), "quiet REF readiness")
        pool.master_board.control.command("gate_iq_reference", {"open": True})
        wait(lambda: all(len(snapshots.get(((bi, a), generations[bi]), {})) == 4 for bi, a in scope.pairs), "reference onset snapshots")
        ref_bi, ref_a = scope.pairs[0]
        ref_packets = snapshots[((ref_bi, ref_a), generations[ref_bi])]
        reference = np.concatenate([ref_packets[k].decode_iq() for k in range(4)])
        ref_edge = ref_packets[0].producer_wake_write_ptr
        anchors, scores = [], []
        for bi in scope.active:
            values = [0] * 8
            for a in scope.ids[bi]:
                packets = snapshots[((bi, a), generations[bi])]
                samples = np.concatenate([packets[k].decode_iq() for k in range(4)])
                lag, score = relative_lag(samples, reference, packets[0].producer_wake_write_ptr - ref_edge)
                values[a] = packets[0].fire_time_ns + round(lag * 1e9 / 80e6)
                scores.append(score)
            anchors.append(values)
        # One sequence names this grid across every board in the pool.
        pool._reference_sequence = (pool._reference_sequence + 1) & 0xFFFFFFFF or 1
        _map(lambda pair: pair[0].iq.set_sync_anchor(pair[1], seq=pool._reference_sequence), zip([pool.boards[bi] for bi in scope.active], anchors))
        scope.apply_rx({"trigger": {"mode": "interval", "interval_chunks": 8192, "burst_chunks": 4}})
        pool.clear_chunks()
        pool._logger.info("Broadband onset correlation min %.3f", min(scores))
        return [pool._reference_sequence] * count
    finally:
        # Restore reference routing even when acquisition or transport fails.
        try:
            pool.master_board.control.command("gate_iq_reference", {"open": True})
        finally:
            for board, handle in subscriptions:
                board.unsubscribe_sensor_messages(handle)


def collect(pool, count=16, timeout=3.0, seqs=None, mask=None):
    """Wait on useful data, not a fixed dwell or diagnostic polling loop."""
    mask = np.ones(pool.shape, bool) if mask is None else mask
    clusters = []
    rejected = dict(configuration=0, weak=0, clipped=0, sync=0)
    event = threading.Event()
    generations = np.array(_map(lambda b: b.iq.get_config()["config_generation"], pool.boards))[:, None, None]

    def cb(c):
        if event.is_set():
            return
        if not np.all((c.config_generation == generations)[mask]):
            rejected["configuration"] += 1
            return
        # Discard quiet/startup data already in transport when TX became ready.
        ac = c.iq[mask] - np.mean(c.iq[mask], axis=-1, keepdims=True)
        if np.min(np.mean(np.abs(ac) ** 2, axis=-1)) < 100:
            rejected["weak"] += 1
            return
        # Phase correction rotates I/Q, so only raw components reveal ADC clipping.
        if np.max(c.adc_peak[mask]) >= 510:
            rejected["clipped"] += 1
            return
        if not np.all(c.sync_info[mask] & (1 << 16)):
            rejected["sync"] += 1
            return
        if seqs is None and len(np.unique(c.sync_info[mask] & 0xFFFF)) != 1:
            rejected["sync"] += 1
            return
        if seqs is not None and not np.all(((c.sync_info & 0xFFFF) == (np.asarray(seqs)[:, None, None] & 0xFFFF))[mask]):
            rejected["sync"] += 1
            return
        if clusters and not np.array_equal(c.fire_time_ns[mask], clusters[0].fire_time_ns[mask]):
            clusters.clear()
        clusters.append(c)
        if len(clusters) >= count:
            event.set()

    handle = pool.add_callback(cb, callback_predicate=lambda c: np.all(c.completion[mask]))
    try:
        deadline = time.monotonic() + timeout
        while not event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = ", ".join(f"{n} {reason}" for reason, n in rejected.items() if n)
                detail = f"; rejected captures: {detail}" if detail else ""
                raise RuntimeError(f"Only {len(clusters)}/{count} synchronized broadband snapshots received{detail}")
            if pool.processing:
                event.wait(remaining)
            else:
                pool._run(timeout=min(0.05, remaining))
        return clusters[:count]
    finally:
        pool.remove_callback(handle)


def fit(clusters, ref_flat, mask=None):
    """Coherence-selected cross-spectral phase and delay, in sample units."""
    x = np.stack([c.iq.reshape(-1, 256) for c in clusters])
    selected = np.ones(x.shape[1], bool) if mask is None else np.asarray(mask).reshape(-1)
    x -= np.mean(x, axis=-1, keepdims=True)
    F = np.fft.fftshift(np.fft.fft(x * np.hanning(256), axis=-1), axes=-1)
    cross = np.mean(F * np.conj(F[:, ref_flat : ref_flat + 1]), axis=0)
    power = np.mean(np.abs(F) ** 2, axis=0)
    coherence = np.abs(cross) ** 2 / np.maximum(power * power[ref_flat], 1e-20)
    f = np.fft.fftshift(np.fft.fftfreq(256))
    taus, phases, qualities = [], [], []
    for a, z in enumerate(cross):
        if not selected[a]:
            taus.append(np.nan)
            phases.append(np.nan)
            qualities.append(np.nan)
            continue
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
