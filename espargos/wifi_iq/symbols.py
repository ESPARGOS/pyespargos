"""Measured OFDM constellation points for a single WiFi IQ reception."""

import numpy as np

from .estimator import _Observation, _PILOTS, _PILOT_SIGNS, _POLARITY, _TONES
from .spatial import _configuration, _decode

_HT_PILOTS = np.array([-21, -7, 7, 21])


def _combine(h, y):
    """Maximum-ratio combination of whitened legacy OFDM observations."""
    h = h[:, :, 0]
    power = np.sum(abs(h) ** 2, axis=0)
    return np.divide(np.sum(h.conj() * y, axis=0), power, out=np.full(power.shape, np.nan + 0j), where=power >= 1)[:, None]


def _symbols(models, offsets, size, tones, pilot_tones, pilot_signs, sections, field):
    pilots = np.isin(tones, pilot_tones)
    for n, offset in enumerate(offsets):
        channels, samples = [], []
        for row, h, hp, gain, noise in models:
            y = row.spectrum(offset, size, gain=gain)
            if y is None:
                continue
            y = y[tones]
            expected = hp[:, 0] * pilot_signs(n)[:, 0]
            cross = y[pilots] * expected.conj()
            if abs(cross.sum()) <= 0.7 * np.sum(abs(cross)):
                continue
            rotation = np.exp(-1j * np.angle(cross.sum()))
            sections["Pilots"].extend((cross * rotation * pilot_signs(n)[:, 0] / np.maximum(abs(expected) ** 2, 1e-12))[abs(expected) > noise])
            channels.append(h / noise)
            samples.append(y * rotation / noise)
        if samples:
            data = _combine(np.array(channels), np.array(samples))[~pilots]
            sections[field].extend(data[np.isfinite(data)])


def symbol_constellation(packets):
    """Return measured soft symbols from one packet for the constellation plot."""
    sections = {k: [] for k in ("Preamble", "L-SIG", "HT-SIG", "Pilots", "Data")}
    if len(packets) and packets[0].phy == 1:
        from ..packet_decoders import dsss_symbols

        p = max(packets, key=lambda packet: np.linalg.norm(packet.barker.channel))
        b = p.barker
        end = b.start + 20 * len(b.signs)
        options = dict(training=b.start, training_words=end - b.start, cfo=b.cfo)
        raw = p.raw.astype("=u4", copy=False)
        sections["Preamble"] = dsss_symbols(raw[:end].tobytes(), field="barker", **options)["points"]
        if p.valid_samples(end, len(raw)) is not None and p._training_gain(b.start, len(raw)) == b.gain:
            sections["Data"] = dsss_symbols(raw.tobytes(), **options)["points"]
        return sections
    models = []
    for p in packets:
        r = _Observation(p)
        a, b = r.fft(0), r.fft(64)
        if a is None or b is None:
            continue
        noise = np.sqrt(max(64 / 6, np.mean(abs(a - b) ** 2) / 2))
        h = r.seed[:, None]
        models.append((r, h, h[_PILOTS], r.gain, noise))
    if not models:
        return sections
    for offset in (0, 64):
        h = np.array([h / noise for _, h, _, _, noise in models])
        y = np.array([r.fft(offset) / noise for r, _, _, _, noise in models])
        z = _combine(h, y)
        sections["Preamble"].extend(z[np.isfinite(z)])
    fixed = lambda n: _PILOT_SIGNS[:, None]
    _symbols(models, [144], 64, _TONES, _HT_PILOTS, fixed, sections, "L-SIG")
    p = models[0][0].packet
    if any(r.packet.phy != p.phy for r, *_ in models):
        return sections
    if p.phy == 0 and p.half_rate in (12, 18, 24, 36, 48, 72, 96, 108):
        count = min(8, (16 + 8 * p.length + 6 + 2 * p.half_rate - 1) // (2 * p.half_rate))
        _symbols(models, 224 + 80 * np.arange(count), 64, _TONES, _HT_PILOTS, lambda n: fixed(n) * _POLARITY[n + 1], sections, "Data")
        return sections
    if p.phy not in (2, 3, 4):
        return sections
    if p.phy == 2:
        _symbols(models, [224, 304], 64, _TONES, _HT_PILOTS, fixed, sections, "HT-SIG")
    # Decode from the strongest usable antenna first. A CRC-checked header
    # supplies the guard interval, training format and spatial stream count.
    for row, *_ in sorted(models, key=lambda m: np.linalg.norm(m[1]) / m[-1], reverse=True):
        p = row.packet
        header = p.phy_header
        expected_phy = 2 if p.phy == 2 else 4
        offsets = (144, 224, 304) if expected_phy == 2 else (144, 224, 304, 384)
        if (
            header
            and header["phy"] == expected_phy
            and abs(header["training_start"] - row.start) <= 4
            and len(header["prefix"]) == (6 if expected_phy == 2 else 7)
            and (expected_phy != 2 or header["length_bytes"] == p.length)
            and all(row.fft(offset) is not None for offset in offsets)
        ):
            break
    else:
        return sections
    settings = _configuration(header)
    if settings is None:
        return sections
    config, streams = settings
    alamouti = config[0] == 2 and streams == 1 and config[1] == 2
    if config[1] != streams and not alamouti:
        return sections
    if config[0] == 4 and (header["he"]["doppler"] or header["he"]["dcm"]):
        return sections
    count = 8
    if config[0] == 2:
        rates = (26, 52, 78, 104, 156, 208, 234, 260)
        count = min(count, int(np.ceil((16 + 8 * p.length + 6) / (streams * rates[header["mcs"] % 8]))))
    result = _decode([(row, noise**2) for row, _, _, _, noise in models], config, count, alamouti)
    if result is not None:
        _, z, _ = result
        tones = np.arange(z.shape[1]) - z.shape[1] // 2
        pilots = np.isin(tones, _HT_PILOTS if config[0] == 2 else [-116, -90, -48, -22, 22, 48, 90, 116])
        for key, mask in (("Data", ~pilots), ("Pilots", pilots)):
            values = z[:, mask]
            sections[key].extend(values[np.isfinite(values)])
    return sections
