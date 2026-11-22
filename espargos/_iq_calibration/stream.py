"""Measure the phase and delay of the narrowband receive path in its live epoch."""
import threading
import time

import numpy as np

from ..iq_rx_stream import IQStreamBlock


def _tone(receiver, scope, center, offset, rate, backoff, timeout):
    receiver.master_board.iq.set_reference("cw", center + offset, backoff)
    generations = {i: receiver.boards[i].iq.get_config()["config_generation"] for i in scope.active}
    frames = []
    wake = threading.Event()
    bins = np.fft.fftfreq(128, 1 / rate)
    window = np.hanning(128)
    # The reference synthesizer rounds the requested RF frequency. Fit the measured tone.
    search = abs(bins - offset) <= 2000
    positions = np.flatnonzero(search)
    epoch = None
    origin = None

    def collect(block):
        nonlocal epoch, origin
        if wake.is_set() or not block.synchronized or block.sample_rate_hz != rate:
            return
        if any(np.any(block.config_generation[i][scope.mask[i]] != g) for i, g in generations.items()):
            return
        iq = block.iq[scope.mask]
        spectrum = np.fft.fft((iq - iq.mean(axis=-1, keepdims=True)) * window, axis=-1)
        power = np.sum(abs(spectrum) ** 2, axis=0)
        peak = int(np.argmax(power))
        if peak not in positions:
            return
        tone = spectrum[:, peak] / window.sum()
        if min(abs(tone)) < 2 or max(abs(tone)) > 480:
            return
        if epoch is None or not np.array_equal(epoch[scope.mask], block.fire_time_ns[scope.mask]):
            epoch, origin = block.fire_time_ns.copy(), block.grid_origin_ns.copy()
            frames.clear()
        frames.append((bins[peak], tone * np.conj(tone[0])))
        if len(frames) == 16:
            wake.set()

    handle = receiver.add_cluster_callback(collect, callback_predicate=lambda c:
        isinstance(c, IQStreamBlock) and np.all(c.completion[scope.mask]))
    try:
        deadline = time.monotonic() + timeout
        while not wake.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"No usable aligned reference tone at {offset:+d} Hz ({len(frames)}/16 blocks)")
            if receiver.processing:
                wake.wait(remaining)
            else:
                receiver._run(timeout=min(remaining, 0.05))
    finally:
        receiver.remove_callback(handle)
    frequency = np.median([f for f, _ in frames])
    z = np.stack([x for _, x in frames])
    mean = z.mean(axis=0)
    coherence = abs(mean) / np.mean(abs(z), axis=0)
    if min(coherence) < 0.98:
        raise RuntimeError(f"Unstable narrowband reference phase (coherence {min(coherence):.3f})")
    return frequency, np.angle(mean), epoch, origin


def measure(receiver, scope, center, rate, backoff, timeout):
    """Fit three tones across the passband and verify on a fourth tone."""
    observations = [_tone(receiver, scope, center, round(x * rate), rate, backoff, timeout)
                    for x in (-0.30, -0.11, 0.30, 0.14)]
    f, phase, epochs, origins = zip(*observations)
    if any(not np.array_equal(t[scope.mask], epochs[0][scope.mask]) for t in epochs[1:]):
        raise RuntimeError("Narrowband receiver restarted during calibration")
    slope, intercept = np.polyfit(f[:3], np.unwrap(phase[:3], axis=0), 1)
    residual = abs(np.angle(np.exp(1j * (phase[3] - (slope * f[3] + intercept)))))
    if max(residual) > 0.15:
        raise RuntimeError(f"Narrowband reference validation residual {max(residual):.3f} rad")
    delay, phi = (np.full(scope.mask.shape, np.nan) for _ in range(2))
    delay[scope.mask], phi[scope.mask] = slope / (2 * np.pi), intercept
    return delay, phi, np.where(scope.mask, epochs[0], 0).tobytes(), origins[0]
