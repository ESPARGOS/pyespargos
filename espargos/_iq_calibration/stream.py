"""Measure the phase and delay of the narrowband receive path in its live epoch."""
import threading
import time

import numpy as np

from ..iq_rx_stream import IQStreamBlock, IQStreamPacket
from ..iq_tone import TONE_MAX_HZ, TONE_MIN_HZ


def _reference_index(receiver, scope):
    board, antenna = scope.pairs[0]
    pos = (board, *receiver.boards[board].revision.antenna_id_to_row_col(antenna))
    return np.flatnonzero(scope.mask.ravel()).searchsorted(np.ravel_multi_index(pos, scope.mask.shape))


def _observe(receiver, scope, center, rate, timeout, count, extract, description):
    generations = {i: receiver.boards[i].iq.get_config()["config_generation"] for i in scope.active}
    frames = []
    wake = threading.Event()
    epoch = origin = None

    def collect(block):
        nonlocal epoch, origin
        if wake.is_set() or not block.synchronized or (block.center_freq_hz, block.sample_rate_hz) != (center, rate):
            return
        if any(np.any(block.config_generation[i][scope.mask[i]] != g) for i, g in generations.items()):
            return
        if epoch is None or not np.array_equal(epoch[scope.mask], block.fire_time_ns[scope.mask]):
            epoch, origin = block.fire_time_ns.copy(), block.grid_origin_ns.copy()
            frames.clear()
        result = extract(block.iq[scope.mask])
        if result is not None:
            frames.append(result)
            if len(frames) == count:
                wake.set()

    handle = receiver.add_cluster_callback(collect, callback_predicate=lambda c:
        isinstance(c, IQStreamBlock) and np.all(c.completion[scope.mask]))
    try:
        deadline = time.monotonic() + timeout
        while not wake.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"No usable aligned {description} ({len(frames)}/{count} blocks)")
            if receiver.processing:
                wake.wait(remaining)
            else:
                receiver._run(timeout=min(remaining, 0.05))
    finally:
        receiver.remove_callback(handle)
    return frames, epoch, origin


def _tone(receiver, scope, center, offset, rate, backoff, timeout):
    receiver.master_board.iq.set_reference("cw", center + offset, backoff)
    reference = _reference_index(receiver, scope)
    n = IQStreamPacket.SAMPLE_COUNT
    bins = np.fft.fftfreq(n, 1 / rate)
    window = np.hanning(n)

    def extract(iq):
        spectrum = np.fft.fft((iq - iq.mean(axis=-1, keepdims=True)) * window, axis=-1)
        power = np.sum(abs(spectrum) ** 2, axis=0)
        peak = int(np.argmax(power))
        if abs(bins[peak] - offset) > 2000:
            return
        tone = spectrum[:, peak] / window.sum()
        if min(abs(tone)) < 2 or max(abs(tone)) > 480:
            return
        # Refine the frequency within one FFT bin using the Hann main lobe.
        left, mid, right = np.log(np.maximum(power[[(peak-1) % n, peak, (peak+1) % n]], 1e-30))
        curvature = left - 2 * mid + right
        fraction = (left - right) / (2 * curvature) if curvature < 0 else 0
        return bins[peak] + fraction * rate / n, tone * np.conj(tone[reference])

    frames, epoch, origin = _observe(receiver, scope, center, rate, timeout, 16, extract, f"reference tone at {offset:+d} Hz")
    frequency = np.median([f for f, _ in frames])
    z = np.stack([x for _, x in frames])
    mean = z.mean(axis=0)
    coherence = abs(mean) / np.mean(abs(z), axis=0)
    if min(coherence) < 0.98:
        raise RuntimeError(f"Unstable narrowband reference phase (coherence {min(coherence):.3f})")
    return frequency, np.angle(mean), epoch, origin


def _fit_noise(blocks, reference):
    """Fit relative phase and delay using the coherent part of the passband."""
    iq = np.stack(blocks)
    n = iq.shape[-1]
    frequency = np.fft.fftfreq(n)
    keep = (abs(frequency) > 0.07) & (abs(frequency) < 0.37)
    frequency = frequency[keep]
    spectrum = np.fft.fft((iq - iq.mean(axis=-1, keepdims=True)) * np.hanning(n), axis=-1)[:, :, keep]
    cross = (spectrum * spectrum[:, reference:reference + 1].conj()).mean(axis=0)
    power = (abs(spectrum) ** 2).mean(axis=0)
    coherence = abs(cross) ** 2 / np.maximum(power * power[reference], 1e-25)
    # Suppress the finite-record noise floor and cap the influence of narrow lines.
    weight = np.maximum(coherence - 3 / len(iq), 0) / np.maximum(1 - coherence, 0.08)
    weight /= np.maximum(weight.sum(axis=1, keepdims=True), 1e-20)
    spread = np.sum(weight * (frequency - (weight * frequency).sum(axis=1, keepdims=True)) ** 2, axis=1)
    if np.any(spread < 0.10 ** 2) or np.any((weight ** 2).sum(axis=1) > 0.20):
        raise RuntimeError("Insufficient coherent bandwidth in the narrowband reference")

    # The coarse synchronization has already aligned the ADC grids. Search the
    # remaining few output samples without phase unwrapping across noisy bins.
    unit = cross / np.maximum(abs(cross), 1e-25)
    delays = np.linspace(-4, 4, 65)
    response = abs((weight * unit) @ np.exp(-2j * np.pi * frequency[:, None] * delays)) ** 2
    peak = response.argmax(axis=1)
    if np.any((peak == 0) | (peak == len(delays) - 1)):
        raise RuntimeError("Narrowband delay exceeds the synchronized capture range")
    rows = np.arange(len(peak))
    left, mid, right = (response[rows, peak + k] for k in (-1, 0, 1))
    fraction = (left - right) / (2 * (left - 2 * mid + right))
    delay = delays[peak] + fraction * (delays[1] - delays[0])
    mean = np.sum(weight * unit * np.exp(-2j * np.pi * delay[:, None] * frequency), axis=1)
    if np.any(~np.isfinite(mean)) or min(abs(mean)) < 0.90:
        raise RuntimeError("Unstable narrowband reference phase")
    return delay, np.angle(mean)


def _measure_noise(receiver, scope, center, rate, timeout):
    # A full-power CW can clip high-gain receivers even at maximum backoff.
    # Spread reference power over the wider analog band and validate the fit
    # with a second, independently tuned reference.
    offsets = (4e6, -4e6)
    if center - 4e6 < TONE_MIN_HZ:
        offsets = (4e6, 6e6)
    elif center + 4e6 > TONE_MAX_HZ:
        offsets = (-4e6, -6e6)
    reference = _reference_index(receiver, scope)
    trials = []
    for offset in offsets:
        receiver.master_board.iq.set_reference("iq", int(center + offset), 0)
        frames, epoch, origin = _observe(receiver, scope, center, rate, timeout, 200, lambda x: x, "noise reference")
        # Discard the reference transition and any buffered samples preceding it.
        delay, phase = _fit_noise(frames[24:], reference)
        trials.append((delay, phase, epoch, origin))
    (d1, p1, epoch, origin), (d2, p2, second_epoch, _) = trials
    if not np.array_equal(epoch[scope.mask], second_epoch[scope.mask]):
        raise RuntimeError("Narrowband receiver restarted during calibration")
    error = abs(np.angle(np.exp(1j * (p1 - p2)))) + 2 * np.pi * 0.375 * abs(d1 - d2)
    if max(error) > 0.22:
        raise RuntimeError(f"Narrowband reference validation residual {max(error):.3f} rad")
    delay, phase = (np.full(scope.mask.shape, np.nan) for _ in range(2))
    delay[scope.mask] = (d1 + d2) / (2 * rate)
    phase[scope.mask] = np.angle(np.exp(1j * p1) + np.exp(1j * p2))
    return delay, phase, np.where(scope.mask, epoch, 0).tobytes(), origin


def measure_at_operating_gain(receiver, scope, configs, center, rate, timeout):
    use_noise = False
    for board in scope.active:
        receivers = []
        for antenna in scope.ids[board]:
            config = configs[board]["receivers"][antenna]
            if config["gain_mode"] == "auto":
                continue
            gain = {key: config[key] for key in ("antid", "gain_mode", "rx_gain", "expert_gain_words") if key in config}
            receivers.append(gain)
            use_noise |= config["gain_mode"] == "expert" or config["rx_gain"] > 30
        if receivers:
            receiver.boards[board].iq.set_config({"receivers": receivers})
    if use_noise:
        return _measure_noise(receiver, scope, center, rate, timeout)
    return measure(receiver, scope, center, rate, 80, timeout)


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
