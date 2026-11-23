"""Measure the phase and delay of the narrowband receive path in its live epoch."""

import threading
import time

import numpy as np

from ..iq_cluster import IQCluster
from ..iq_packet import IQ_CHUNK_SAMPLE_WORDS, IQ_SYNC_INFO_GRID_SYNCED, IQ_SYNC_INFO_FIRE_TIME_VALID, IQ_SYNC_INFO_SEQ_MASK
from ..iq_rx_stream import IQStreamBlock, IQStreamPacket
from ..iq_tone import TONE_MAX_HZ, TONE_MIN_HZ


def _reference_index(receiver, scope):
    board, antenna = scope.pairs[0]
    pos = (board, *receiver.boards[board].revision.antenna_id_to_row_col(antenna))
    return np.flatnonzero(scope.mask.ravel()).searchsorted(np.ravel_multi_index(pos, scope.mask.shape))


def _observe(receiver, scope, center, rate, timeout, count, extract, description):
    configs = {i: receiver.boards[i].iq.get_config() for i in scope.active}
    frames = []
    wake = threading.Event()
    epoch = origin = None

    def collect(block):
        nonlocal epoch, origin
        if wake.is_set():
            return
        if isinstance(block, IQStreamBlock):
            valid = block.synchronized and (block.center_freq_hz, block.sample_rate_hz) == (center, rate)
        else:
            flags = IQ_SYNC_INFO_GRID_SYNCED | IQ_SYNC_INFO_FIRE_TIME_VALID
            valid = np.all((block.sync_info[scope.mask] & flags) == flags) and np.all(block.center_freq_hz[scope.mask] == center) and np.all(block.sample_rate_hz[scope.mask] == rate)
        if not valid:
            return
        for i, config in configs.items():
            if np.any(block.config_generation[i][scope.mask[i]] != config["config_generation"]):
                return
            seq = config.get("sync_seq")
            if seq is not None:
                actual = block.sync_info if isinstance(block, IQStreamBlock) else block.sync_info[i][scope.mask[i]]
                if not seq or np.any((actual & IQ_SYNC_INFO_SEQ_MASK) != (seq & IQ_SYNC_INFO_SEQ_MASK)):
                    return
        if epoch is None or not np.array_equal(epoch[scope.mask], block.fire_time_ns[scope.mask]):
            epoch, origin = block.fire_time_ns.copy(), block.grid_origin_ns.copy()
            frames.clear()
        result = extract(block.iq[scope.mask])
        if result is not None:
            frames.append(result)
            if len(frames) == count:
                wake.set()

    handle = receiver.add_cluster_callback(collect, callback_predicate=lambda c: isinstance(c, (IQStreamBlock, IQCluster)) and np.all(c.completion[scope.mask]))
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
    n = IQStreamPacket.SAMPLE_COUNT if rate <= 100000 else IQ_CHUNK_SAMPLE_WORDS
    bins = np.fft.fftfreq(n, 1 / rate)
    window = np.hanning(n)

    def extract(iq):
        if np.any(np.abs(iq.real) >= 510) or np.any(np.abs(iq.imag) >= 510):
            return
        spectrum = np.fft.fft((iq - iq.mean(axis=-1, keepdims=True)) * window, axis=-1)
        power = np.sum(abs(spectrum) ** 2, axis=0)
        peak = int(np.argmax(power))
        if abs(bins[peak] - offset) > max(2000, rate / n):
            return
        tone = spectrum[:, peak] / window.sum()
        if min(abs(tone)) < (2 if rate <= 100000 else 0.5) or max(abs(tone)) > 480:
            return
        # Refine the frequency within one FFT bin using the Hann main lobe.
        left, mid, right = np.log(np.maximum(power[[(peak - 1) % n, peak, (peak + 1) % n]], 1e-30))
        curvature = left - 2 * mid + right
        fraction = (left - right) / (2 * curvature) if curvature < 0 else 0
        return bins[peak] + fraction * rate / n, tone * np.conj(tone[reference])

    frames, epoch, origin = _observe(receiver, scope, center, rate, timeout, 16, extract, f"reference tone at {offset:+d} Hz")
    frequency = np.median([f for f, _ in frames])
    z = np.stack([x for _, x in frames])
    mean = z.mean(axis=0)
    coherence = abs(mean) / np.mean(abs(z), axis=0)
    if min(coherence) < 0.98:
        worst = tuple(np.argwhere(scope.mask)[np.argmin(coherence)])
        raise RuntimeError(f"Unstable narrowband reference phase on {receiver.boards[worst[0]].host} " f"row {worst[1]}, column {worst[2]} (coherence {min(coherence):.3f})")
    return frequency, np.angle(mean), epoch, origin


def _fit_noise(blocks, reference, bandwidth_fraction=1.0):
    """Fit relative phase and delay using the coherent part of the passband."""
    iq = np.stack(blocks)
    n = iq.shape[-1]
    frequency = np.fft.fftfreq(n)
    keep = (abs(frequency) > 0.07 * bandwidth_fraction) & (abs(frequency) < 0.37 * bandwidth_fraction)
    frequency = frequency[keep]
    spectrum = np.fft.fft((iq - iq.mean(axis=-1, keepdims=True)) * np.hanning(n), axis=-1)[:, :, keep]
    cross = (spectrum * spectrum[:, reference : reference + 1].conj()).mean(axis=0)
    power = (abs(spectrum) ** 2).mean(axis=0)
    coherence = abs(cross) ** 2 / np.maximum(power * power[reference], 1e-25)
    # Suppress the finite-record noise floor and cap the influence of narrow lines.
    weight = np.maximum(coherence - 3 / len(iq), 0) / np.maximum(1 - coherence, 0.08)
    weight /= np.maximum(weight.sum(axis=1, keepdims=True), 1e-20)
    spread = np.sum(weight * (frequency - (weight * frequency).sum(axis=1, keepdims=True)) ** 2, axis=1)
    if np.any(spread < (0.10 * bandwidth_fraction) ** 2) or np.any((weight**2).sum(axis=1) > 0.20):
        raise RuntimeError("Insufficient coherent bandwidth in the narrowband reference")

    # The coarse synchronization has already aligned the ADC grids. Search the
    # remaining delay in units of inverse passband width. This stays well
    # conditioned for a 1.2 MHz analog filter sampled at 32 MS/s.
    unit = cross / np.maximum(abs(cross), 1e-25)
    delays = np.linspace(-4, 4, 65) / bandwidth_fraction
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


def _measure_noise(receiver, scope, center, rate, timeout, bandwidth_fraction=1.0, backoff=0, spacing=4000000):
    # A full-power CW can clip high-gain receivers even at maximum backoff.
    # Spread reference power over the wider analog band and validate the fit
    # with a second, independently tuned reference.
    outer = round(1.5 * spacing / 1000) * 1000
    offsets = (spacing, -spacing)
    if center - spacing < TONE_MIN_HZ:
        offsets = (spacing, outer)
    elif center + spacing > TONE_MAX_HZ:
        offsets = (-spacing, -outer)
    reference = _reference_index(receiver, scope)
    trials = []
    for offset in offsets:
        receiver.master_board.iq.set_reference("iq", int(center + offset), backoff)
        frames, epoch, origin = _observe(receiver, scope, center, rate, timeout, 200, lambda x: x, "noise reference")
        # Discard the reference transition and any buffered samples preceding it.
        delay, phase = _fit_noise(frames[24:], reference, bandwidth_fraction)
        trials.append((delay, phase, epoch, origin))
    (d1, p1, epoch, origin), (d2, p2, second_epoch, _) = trials
    if not np.array_equal(epoch[scope.mask], second_epoch[scope.mask]):
        raise RuntimeError("Narrowband receiver restarted during calibration")
    error = abs(np.angle(np.exp(1j * (p1 - p2)))) + 2 * np.pi * 0.375 * bandwidth_fraction * abs(d1 - d2)
    if max(error) > 0.22:
        raise RuntimeError(f"Narrowband reference validation residual {max(error):.3f} rad")
    delay, phase = (np.full(scope.mask.shape, np.nan) for _ in range(2))
    delay[scope.mask] = (d1 + d2) / (2 * rate)
    phase[scope.mask] = np.angle(np.exp(1j * p1) + np.exp(1j * p2))
    return delay, phase, np.where(scope.mask, epoch, 0).tobytes(), origin


def measure_bluetooth(receiver, scope, configs, center, rate, timeout, attempt, gain_override=None):
    config = configs[scope.active[0]]
    streaming = config["trigger"]["mode"] == "downsample"
    bandwidth = min(rate, 1000 * config["bluetooth_filter_khz"])
    prefer_noise = False
    for board in scope.active:
        receivers = []
        for antenna in scope.ids[board]:
            settings = configs[board]["receivers"][antenna]
            selected = {key: settings[key] for key in ("antid", "gain_mode", "rx_gain", "expert_gain_words") if key in settings}
            if gain_override is not None:
                selected.update(gain_mode="manual", rx_gain=gain_override)
            receivers.append(selected)
            prefer_noise |= selected["gain_mode"] == "expert" or (selected["gain_mode"] == "manual" and selected["rx_gain"] >= 30)
        receiver.boards[board].iq.set_config({"receivers": receivers})

    # A raw capture covers the analog passband. Independent broadband probes
    # avoid the compression seen with a CW even at modest receiver gains.
    # Vary the reference power on retries, preserving the measured RF state.
    if streaming and prefer_noise and rate <= 16000:
        # Reduce compression first, then move to an occupied OFDM subcarrier.
        trial = attempt % 3
        return _measure_noise(receiver, scope, center, rate, timeout, bandwidth / rate, (80, 40, 0)[trial], 4000000 if trial == 0 else 3125000)
    if streaming:
        noise = attempt % 3 == (0 if prefer_noise else 1)
    else:
        noise = attempt % 3 != 1
    if noise:
        backoff = 40 if not streaming and attempt % 3 == 2 else 0
        return _measure_noise(receiver, scope, center, rate, timeout, bandwidth / rate, backoff)
    backoff = 80 if attempt % 3 < 2 else 0
    return measure(receiver, scope, center, rate, backoff, timeout, bandwidth=bandwidth)


def measure(receiver, scope, center, rate, backoff, timeout, bandwidth=None):
    """Fit three tones across the passband and verify on a fourth tone."""
    # Alternate sides so buffered samples from the previous tone cannot pass
    # the next tone's frequency check, including at the lowest output rate.
    bandwidth = bandwidth or rate
    offsets = (-0.30, 0.30, -0.13, 0.16)
    # At either end of the RF tuning range, fit tones on the available side.
    if center - 0.30 * bandwidth < TONE_MIN_HZ:
        offsets = (0.16, 0.36, 0.09, 0.26)
    elif center + 0.30 * bandwidth > TONE_MAX_HZ:
        offsets = (-0.16, -0.36, -0.09, -0.26)
    observations = [_tone(receiver, scope, center, round(x * bandwidth / 1000) * 1000, rate, backoff, timeout) for x in offsets]
    f, phase, epochs, origins = zip(*observations)
    if any(not np.array_equal(t[scope.mask], epochs[0][scope.mask]) for t in epochs[1:]):
        raise RuntimeError("Narrowband receiver restarted during calibration")
    order = np.argsort(f[:3])
    slope, intercept = np.polyfit(np.asarray(f)[order], np.unwrap(np.asarray(phase)[order], axis=0), 1)
    predicted = slope * np.asarray(f)[:, None] + intercept
    errors = np.abs(np.angle(np.exp(1j * (np.asarray(phase) - predicted))))
    residual = errors.max()
    if residual > 0.15:
        pilot, channel = np.unravel_index(errors.argmax(), errors.shape)
        bi, row, col = np.argwhere(scope.mask)[channel]
        raise RuntimeError(f"Narrowband reference validation residual {residual:.3f} rad on " f"{receiver.boards[bi].host} row {row}, column {col} at {f[pilot]:+.0f} Hz")
    delay, phi = (np.full(scope.mask.shape, np.nan) for _ in range(2))
    delay[scope.mask], phi[scope.mask] = slope / (2 * np.pi), intercept
    return delay, phi, np.where(scope.mask, epochs[0], 0).tobytes(), origins[0]
