"""Estimate the 802.11b channel from the Barker-spread SYNC field at 20 MSa/s."""

from dataclasses import dataclass

import numpy as np

from ..packet_decoders import wifi

# One 11-chip Barker symbol interpolated to 20 MSa/s, including adjacent
# symbol tails. This is the same reference pulse used by the browser.
# fmt: off
_PULSE = np.array([
        0, 0, -0.0004118203346, -0.002178209318, 0.003222960665, 0.006154214811,
        -0.01185812162, -0.00972320567, 0.02683615103, 0.005927249336, -0.04776933549, 0.009372060673,
        0.0715840214, -0.04342005768, -0.09775928571, 0.1079459468, 0.1166761276, -0.2386639883,
        -0.1227004656, 0.73532959, 1.000673607, -0.1127374361, -1.056115547, -0.1562868252,
        1.543813454, 1.645465896, 0.005560161148, -1.053159694, -0.2689384478, 0.9359316717,
        1.150908531, 1.001837774, 1.159855664, 0.7951478794, -0.4195116129, -1.246189793,
        -1.112197883, -0.9298418424, -1.01453297, -0.6742744956, 0, 0.196385667,
        -0.04394223377, -0.09350767018, 0.0458005913, 0.04186703504, -0.03573920626, -0.01528568173,
        0.02355578596, 0.002934372506, -0.01444655461, 0.00141487944, 0.006135157068, -0.001587976427,
        -0.002113501538, 0.001901738331, 0.0008859650764, 0, 0, 0,
    ])
# fmt: on
_BINS = np.arange(-17, 18)
_RESPONSE = np.exp(-2j * np.pi / 64 * np.outer(_BINS, np.arange(-4, 5)))


@dataclass
class BarkerTraining:
    start: int
    cfo: float
    gain: int
    channel: np.ndarray
    signs: np.ndarray

    def align(self, reference):
        """Match transmitted DBPSK signs to resolve polarity and whole-symbol offset."""
        n, m = len(self.signs), len(reference.signs)
        lags = np.arange(1 - m, n)
        overlap = np.minimum(n, m + lags) - np.maximum(0, lags)
        corr = np.correlate(self.signs, reference.signs, "full")
        valid = (overlap >= min(12, n, m)) & (abs(corr) >= 0.85 * overlap)
        if not valid.any():
            return None
        scores = np.where(valid, abs(corr), 0)
        best = scores.argmax()
        # Short accidental matches at an edge must not move the array timing.
        if np.count_nonzero(scores >= scores[best] - 2) != 1:
            return None
        offset = int(lags[best]) * 20
        return offset, np.sign(corr[best]) * np.exp(2j * np.pi * self.cfo * offset / 20e6)


def barker_training(p):
    start, count, cfo = p.training, p.training_words, p.cfo
    if not count:
        result = wifi(p.raw[: p.frame].tobytes(), stage="native_dsss", training_hint=start if start != 65535 else 0)
        if result is None:
            return None
        start, count, cfo = result["training_start"], result["training_words"], result["cfo_hz"]
    else:
        # PLCP decoding locates SYNC, but can land between chips. Find the
        # strongest Barker phase in the interior before fitting the channel.
        interior = count // 20 - 2
        if interior < 8:
            return None
        offsets = np.arange(-10, 10)
        pulse = _PULSE.reshape(3, 20).sum(axis=0)
        window = p.samples[start + 10 : start + 30 + interior * 20]
        windows = np.lib.stride_tricks.sliding_window_view(window, 20)
        peaks = windows[np.arange(interior)[:, None] * 20 + offsets + 10] @ pulse
        start += int(offsets[np.sum(abs(peaks) ** 2, axis=0).argmax()])
        if start < 0:
            start += 20
        count = min(count, len(p.raw) - start)
    count = count // 20 * 20
    if count < 200:
        return None
    x = p.valid_samples(start, start + count)
    gain = p._training_gain(start, start + count)
    if x is None or gain is None:
        return None
    x = x * np.exp(-2j * np.pi * cfo / p.sample_rate * np.arange(count))
    x -= x.mean()
    # A repeated all-positive Barker sequence includes both neighboring tails.
    matched = x.reshape(-1, 20) @ _PULSE.reshape(3, 20).sum(axis=0)
    # Estimate the BPSK axis from every symbol. The remaining sign ambiguity
    # is resolved against another antenna observing the same transmitted bits.
    axis = np.exp(0.5j * np.angle(np.sum(matched**2)))
    signs = np.where((matched * axis.conjugate()).real >= 0, 1.0, -1.0)
    impulses = np.zeros(count)
    impulses[::20] = signs
    template = np.convolve(impulses, _PULSE)[20 : 20 + count]
    taps = np.lib.stride_tricks.sliding_window_view(template, 9)[20:-20, ::-1].copy()
    taps -= taps.mean(axis=0)
    y = x[24:-24] - x[24:-24].mean()
    gram = taps.T @ taps
    gram.flat[::10] += np.trace(gram) * 1e-4 / 9
    fit = np.linalg.solve(gram, taps.T @ y)
    if np.linalg.norm(y - taps @ fit) >= 0.7 * np.linalg.norm(y):
        return None
    return BarkerTraining(start, cfo, gain, (_RESPONSE @ fit).astype(np.complex64), signs)
