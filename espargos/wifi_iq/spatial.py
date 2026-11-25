"""Channel responses from spatially encoded HT20 and HE SU20 training fields."""

from dataclasses import dataclass

import numpy as np

from ..packet_decoders import spatial_decode, spatial_layout
from .estimator import _Observation


def _configuration(header):
    phy = header["phy"]
    if phy == 2:
        sig = header["prefix"]
        streams = header["mcs"] // 8 + 1
        paths = streams + header["stbc"]
        if sig[0] & 128 or sig[4] & 3 or paths > 4:
            return None
        return (phy, paths, 1, 8 if sig[3] & 128 else 16), streams
    if phy == 4:
        he = header["he"]
        if he["bandwidth_mhz"] == 20 and 1 <= he["space_time_streams"] <= 8:
            return (phy, he["space_time_streams"], he["ltf_size"], he["guard_interval_ns"] // 50), he["spatial_streams"]
    return None


def _decode(rows, config, max_symbols, alamouti=False):
    paths = config[1]
    count, size, first, stride, fft, data, period = spatial_layout(*config)
    spectra, noises, gains = [], [], []
    for row, noise in rows:
        gain = row.packet._training_gain(row.start + first, row.start + first + size)
        if gain is None:
            continue
        training = [row.spectrum(first + n * stride, size, gain=gain) for n in range(count)]
        if any(field is None for field in training):
            continue
        data_fields = np.full((max_symbols, fft), np.nan + 0j)
        for n in range(max_symbols):
            field = row.spectrum(data + n * period, fft, gain=gain)
            if field is not None:
                data_fields[n] = field
        spectra.append(np.concatenate([*training, data_fields.ravel()]))
        noises.append(noise)
        gains.append(gain)
    if not gains:
        return None
    h, z, _, _ = spatial_decode(*config, alamouti, max_symbols, np.asarray(noises, dtype="d"), np.asarray(spectra, dtype=np.complex128))
    channels = np.frombuffer(h, dtype=np.complex128).reshape(len(gains), fft, paths)
    symbols = np.frombuffer(z, dtype=np.complex128).reshape(max_symbols, fft, 1 if alamouti else paths)
    return np.fft.fftshift(channels, axes=1), np.fft.fftshift(symbols, axes=1), gains


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
    settings = _configuration(header)
    if settings is None:
        return None
    config, streams = settings
    result = _decode([(row, 1.0)], config, 0)
    if result is None:
        return None
    h, _, gains = result
    h = h[0]
    measured = np.isfinite(h[:, 0])
    tones = np.arange(len(h)) - len(h) // 2
    return SpatialCSI("HT20" if config[0] == 2 else "HE20", header["mcs"], streams, tones[measured] * (20000000 // len(h)), h[measured].T.astype(np.complex64), gains[0])
