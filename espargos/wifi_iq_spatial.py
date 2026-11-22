"""Channel responses from spatially encoded HT20 and HE SU20 training fields."""

from dataclasses import dataclass
from functools import cache

import numpy as np

from .wifi_iq_estimator import _HT_LTF, _Observation

_HT_TONES = np.r_[-28:0, 1:29]
_HT_SIGNS = np.r_[1, 1, _HT_LTF, -1, -1]
_HT_PILOTS = np.array([-21, -7, 7, 21])
_HE_TONES = np.r_[-122:-1, 2:123]
_HE_PILOTS = np.array([-116, -90, -48, -22, 22, 48, 90, 116])
# HE SU20 training sequences in ascending subcarrier order (0 marks DC).
_HE_TRAINING = {
    4: "--+-+-+++-+++--+-----++----++-+-++++-+--++-++++--+---++++-++----+--++-+----+-+------++-----+--+++-+++-+-+-----+++---+-+++000-+-+-++-+++--+--+-+-+++-+++--+-----++------+-+----+-++--+----++-+++++++-++----+--++-+----+-+--++++--+++++-++---+---+-+-++",
    2: "---++-+----+-+--++-+++++-+-+--++-+----+-+++--+-----+---+++--+0+-++-++-++--+-++++-+-++--+-----+-++--++-+----+-+++--+-----+-+",
    1: "-++-+-++++--+++---+--++--+--+-0-++++++-----+---+--+--+-+-----",
}


def _ht_model(row, streams):
    # Three space-time streams use a four-symbol training matrix.
    count = (1, 2, 4, 4)[streams - 1]
    gain = row.packet._training_gain(row.start + 464, row.start + 528)
    if gain is None:
        return None
    fields = [row.spectrum(464 + 80 * n, gain=gain) for n in range(count)]
    if any(field is None for field in fields):
        return None
    h = (np.stack(fields, axis=1)[_HT_TONES] @ _he_matrix(count)[:streams].T
            * (_HT_SIGNS / count)[:, None])
    return h, gain


@cache
def _he_matrix(n):
    if n == 6:
        i = np.arange(n)
        return np.exp(-2j * np.pi * np.outer(i, i) / n) * [1, -1, 1, 1, 1, -1]
    p = np.array([[1, -1, 1, 1], [1, 1, -1, 1], [1, 1, 1, -1], [-1, 1, 1, 1]])
    return np.block([[p, p], [p, -p]]) if n == 8 else p[:n, :n]


@cache
def _he_training(ltf):
    step = 4 // ltf
    signs = np.array([{"+": 1, "-": -1, "0": 0}[x] for x in _HE_TRAINING[ltf]])
    tones = (np.arange(len(signs)) - len(signs) // 2) * step
    separate = (signs != 0) & ~np.isin(tones, _HE_PILOTS)
    return tones, signs, separate


def _he_fields(row, streams, count, ltf, gi):
    """Separate the measured LTFs before interpolation or pilot recovery."""
    size, step = 64 * ltf, 4 // ltf
    first = 528 + gi
    gain = row.packet._training_gain(row.start + first, row.start + first + size)
    if gain is None:
        return None
    fields = [row.spectrum(first + j * (size + gi), size, gain=gain) for j in range(count)]
    if any(x is None for x in fields):
        return None
    spectra = np.stack(fields, axis=1)
    tones, signs, _ = _he_training(ltf)
    measured = spectra[tones // step] @ _he_matrix(count)[:streams].conj().T
    measured *= signs[:, None] * np.sqrt(step) / count
    return measured, spectra, gain


def _he_model(row, streams, count, ltf, gi):
    fields = _he_fields(row, streams, count, ltf, gi)
    if fields is None:
        return None
    measured, spectra, gain = fields
    tones, _, separate = _he_training(ltf)
    step = 4 // ltf
    h = np.full((len(_HE_TONES), streams), np.nan + 0j)
    for side in (-1, 1):
        trained = separate & (tones * side > 0)
        use = _HE_TONES * side > 0
        for j in range(streams):
            h[use, j] = np.interp(_HE_TONES[use], tones[trained], measured[trained, j], left=np.nan, right=np.nan)
    # HE pilots share the first LTF code across streams.
    pilot = h[np.isin(_HE_TONES, _HE_PILOTS)].sum(axis=1)
    for j, k in enumerate(_HE_PILOTS):
        if ltf != 1 and k in tones:
            pilot[j] = measured[tones == k, 0].item()
    size = len(spectra)
    guard = abs(np.fft.fftfreq(size) * size) > 122 / step
    guard_noise = np.sqrt(step * np.mean(abs(spectra[guard]) ** 2))
    return h, pilot, gain, guard_noise


@dataclass(frozen=True)
class SpatialCSI:
    """Effective space-time channels. Frequency offsets are in Hz relative to the tuned center.

    The last two axes of coefficients are (space-time stream, frequency bin).
    Transmit spatial mapping and cyclic shifts remain part of the channel.
    """
    phy: str
    mcs: int
    spatial_streams: int
    frequencies_hz: np.ndarray
    coefficients: np.ndarray
    rx_gain: int | np.ndarray


def measure(packet):
    header = packet.phy_header
    if header is None or packet.ofdm is None:
        return None
    row = _Observation(packet)
    if abs(header["training_start"] - row.start) > 4:
        return None
    if header["phy"] == 2:
        sig = header["prefix"]
        streams = header["mcs"] // 8 + 1
        paths = streams + header["stbc"]
        if sig[0] & 128 or sig[4] & 3 or paths > 4:
            return None
        model = _ht_model(row, paths)
        if model is not None:
            h, gain = model
            return SpatialCSI("HT20", header["mcs"], streams, _HT_TONES * 312500, h.T.astype(np.complex64), gain)
    else:
        cfg = header["he"]
        paths = cfg["space_time_streams"]
        if cfg["bandwidth_mhz"] != 20 or not 1 <= paths <= 8:
            return None
        count = (1, 2, 4, 4, 6, 6, 8, 8)[paths - 1]
        ltf = cfg["ltf_size"]
        fields = _he_fields(row, paths, count, ltf, cfg["guard_interval_ns"] // 50)
        if fields is not None:
            h, _, gain = fields
            tones, _, separate = _he_training(ltf)
            return SpatialCSI("HE20", header["mcs"], cfg["spatial_streams"],
                              tones[separate] * 78125, h[separate].T.astype(np.complex64), gain)
    return None
