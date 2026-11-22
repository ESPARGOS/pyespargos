"""Measure the phase and delay of the narrowband receive path in its live epoch."""
import threading
import time

import numpy as np

from ..iq_rx_stream import IQStreamBlock, IQStreamPacket
from ..iq_tone import TONE_MAX_HZ, TONE_MIN_HZ


def _tone(receiver, scope, center, offset, rate, backoff, timeout):
    receiver.master_board.iq.set_reference("cw", center + offset, backoff)
    generations = {i: receiver.boards[i].iq.get_config()["config_generation"] for i in scope.active}
    frames = []
    wake = threading.Event()
    n = IQStreamPacket.SAMPLE_COUNT
    bins = np.fft.fftfreq(n, 1 / rate)
    window = np.hanning(n)
    board, antenna = scope.pairs[0]
    pos = (board, *receiver.boards[board].revision.antenna_id_to_row_col(antenna))
    reference = np.flatnonzero(scope.mask.ravel()).searchsorted(np.ravel_multi_index(pos, scope.mask.shape))
    # The reference synthesizer rounds the requested RF frequency. Fit the measured tone.
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
        if abs(bins[peak] - offset) > 2000:
            return
        tone = spectrum[:, peak] / window.sum()
        if min(abs(tone)) < 2 or max(abs(tone)) > 480:
            return
        if epoch is None or not np.array_equal(epoch[scope.mask], block.fire_time_ns[scope.mask]):
            epoch, origin = block.fire_time_ns.copy(), block.grid_origin_ns.copy()
            frames.clear()
        # Refine the frequency within one FFT bin using the Hann main lobe.
        left, mid, right = np.log(np.maximum(power[[(peak-1) % n, peak, (peak+1) % n]], 1e-30))
        curvature = left - 2 * mid + right
        fraction = (left - right) / (2 * curvature) if curvature < 0 else 0
        frames.append((bins[peak] + fraction * rate / n, tone * np.conj(tone[reference])))
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
    # Alternate sides so buffered samples from the previous tone cannot pass
    # the next tone's frequency check, including at the lowest output rate.
    offsets = (-0.30, 0.30, -0.13, 0.16)
    # At either end of the RF tuning range, fit tones on the available side.
    if center - 0.30 * rate < TONE_MIN_HZ:
        offsets = (0.16, 0.36, 0.09, 0.26)
    elif center + 0.30 * rate > TONE_MAX_HZ:
        offsets = (-0.16, -0.36, -0.09, -0.26)
    observations = [_tone(receiver, scope, center, round(x * rate), rate, backoff, timeout)
                    for x in offsets]
    f, phase, epochs, origins = zip(*observations)
    if any(not np.array_equal(t[scope.mask], epochs[0][scope.mask]) for t in epochs[1:]):
        raise RuntimeError("Narrowband receiver restarted during calibration")
    order = np.argsort(f[:3])
    slope, intercept = np.polyfit(np.asarray(f)[order], np.unwrap(np.asarray(phase)[order], axis=0), 1)
    residual = abs(np.angle(np.exp(1j * (phase[3] - (slope * f[3] + intercept)))))
    if max(residual) > 0.15:
        raise RuntimeError(f"Narrowband reference validation residual {max(residual):.3f} rad")
    delay, phi = (np.full(scope.mask.shape, np.nan) for _ in range(2))
    delay[scope.mask], phi[scope.mask] = slope / (2 * np.pi), intercept
    return delay, phi, np.where(scope.mask, epochs[0], 0).tobytes(), origins[0]
