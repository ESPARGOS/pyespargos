"""Measured OFDM constellation points for a single WiFi IQ reception."""

import numpy as np

from .wifi_iq_estimator import _Observation, _PILOTS, _PILOT_SIGNS, _POLARITY, _TONES

from .wifi_iq_spatial import _HT_TONES, _HT_PILOTS, _HE_TONES, _HE_PILOTS, _ht_model, _he_model


def _combine(h, y):
    """Noise-whitened least squares on each tone. Omit unsupported dimensions."""
    if h.shape[-1] == 1:
        h = h[:, :, 0]
        power = np.sum(abs(h) ** 2, axis=0)
        return np.divide(np.sum(h.conj() * y, axis=0), power, out=np.full(power.shape, np.nan + 0j), where=power >= 1)[:, None]
    h = h.transpose(1, 0, 2)
    valid = np.isfinite(h).all(axis=(1, 2))
    result = np.full((len(h), h.shape[-1]), np.nan + 0j)
    if valid.any() and h.shape[1] >= h.shape[2]:
        u, s, vh = np.linalg.svd(h[valid], full_matrices=False)
        strong = (s[:, -1] > 0.05 * s[:, 0]) & (s[:, -1] ** 2 > 5)
        z = (u[strong].conj().transpose(0, 2, 1) @ y.T[valid][strong, :, None]) / s[strong, :, None]
        result[np.flatnonzero(valid)[strong]] = (vh[strong].conj().transpose(0, 2, 1) @ z)[:, :, 0]
    return result


def _symbols(models, offsets, size, tones, pilot_tones, pilot_signs, sections, field, stbc=False):
    pilots = np.isin(tones, pilot_tones)
    previous = {}
    for n, offset in enumerate(offsets):
        channels, samples, pairs = [], [], []
        if n % 2 == 0:
            previous.clear()
        for i, (row, h, hp, gain, noise) in enumerate(models):
            y = row.spectrum(offset, size, gain=gain)
            if y is None:
                continue
            y = y[tones]
            expected = (hp * pilot_signs(n)).sum(axis=1)
            cross = y[pilots] * expected.conj()
            if abs(cross.sum()) <= 0.7 * np.sum(abs(cross)):
                continue
            rotation = np.exp(-1j * np.angle(cross.sum()))
            sections["Pilots"].extend((y[pilots] * rotation * expected.conj() / np.maximum(abs(expected) ** 2, 1e-12) * pilot_signs(n)[:, 0])[abs(expected) > noise])
            y = y * rotation / noise
            if stbc:
                if n % 2 == 0:
                    previous[i] = y
                    continue
                if i not in previous:
                    continue
                pairs.append(previous[i])
            channels.append(h / noise)
            samples.append(y)
        if not samples:
            continue
        h, y = np.array(channels), np.array(samples)
        if stbc:
            a, b = h[:, :, 0], h[:, :, 1]
            u = np.array(pairs)
            power = (abs(a) ** 2 + abs(b) ** 2).sum(axis=0)
            z = np.stack([(a.conj() * u + b * y.conj()).sum(axis=0), (a.conj() * y - b * u.conj()).sum(axis=0)], axis=1)
            z /= np.where(power >= 1, power, np.nan)[:, None]
            previous.clear()
        else:
            z = _combine(h, y)
        data = z[~pilots]
        if h.shape[-1] == 1 or np.isfinite(data).sum() >= data.size // 2:
            sections[field].extend(data[np.isfinite(data)])


def symbol_constellation(packets):
    """Return measured soft symbols from one packet for the constellation plot."""
    sections = {k: [] for k in ("Preamble", "L-SIG", "HT-SIG", "Pilots", "Data")}
    if len(packets) and packets[0].phy == 1:
        from .packet_decoders import dsss_symbols

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
        if (header and header["phy"] == expected_phy and abs(header["training_start"] - row.start) <= 4
                and len(header["prefix"]) == (6 if expected_phy == 2 else 7)
                and (expected_phy != 2 or header["length_bytes"] == p.length)
                and all(row.fft(offset) is not None for offset in offsets)):
            break
    else:
        return sections
    sig = header["prefix"]
    if p.phy == 2:
        mcs = header["mcs"]
        streams = mcs // 8 + 1
        stbc = header["stbc"]
        if sig[0] & 128 or sig[4] & 3 or streams > 4 or stbc and (streams != 1 or stbc != 1):
            return sections
        paths = streams + stbc
        stride = 72 if sig[3] & 128 else 80
        rates = (26, 52, 78, 104, 156, 208, 234, 260)
        count = min(8, int(np.ceil((16 + 8 * p.length + 6) / (streams * rates[mcs % 8]))))
        ht = []
        for r, _, _, _, noise in models:
            model = _ht_model(r, paths)
            if model is not None:
                h, gain = model
                ht.append((r, h, h[np.isin(_HT_TONES, _HT_PILOTS)], gain, noise))
        # IEEE 802.11n Table 20-18, ordered by space-time stream.
        patterns = (
            [[1, 1, 1, -1]],
            [[1, 1, -1, -1], [1, -1, -1, 1]],
            [[1, 1, -1, -1], [1, -1, 1, -1], [-1, 1, 1, -1]],
            [[1, 1, 1, -1], [1, 1, -1, 1], [1, -1, 1, 1], [-1, 1, 1, 1]],
        )
        pattern = np.asarray(patterns[paths - 1]).T
        ltf_count = (1, 2, 4, 4)[paths - 1]
        _symbols(ht, 464 + 80 * (ltf_count - 1) + stride * (1 + np.arange(count)), 64, _HT_TONES, _HT_PILOTS, lambda n: np.roll(pattern, -n, axis=0) * _POLARITY[n + 3], sections, "Data", bool(stbc))
    else:
        cfg = header["he"]
        if cfg["bandwidth_mhz"] != 20 or cfg["space_time_streams"] != cfg["spatial_streams"] or cfg["doppler"] or cfg["dcm"]:
            return sections
        streams = cfg["spatial_streams"]
        count = (1, 2, 4, 4, 6, 6, 8, 8)[streams - 1]
        ltf, gi = cfg["ltf_size"], cfg["guard_interval_ns"] // 50
        he = []
        for r, _, _, _, noise in models:
            model = _he_model(r, streams, count, ltf, gi)
            if model:
                h, pilot, gain, guard_noise = model
                he.append((r, h, pilot[:, None], gain, max(2 * noise, guard_noise)))
        offsets = 528 + count * (64 * ltf + gi) + gi + np.arange(8) * (256 + gi)
        base = np.array([1, 1, 1, -1, -1, 1, 1, 1])[:, None]
        _symbols(he, offsets, 256, _HE_TONES, _HE_PILOTS, lambda n: np.roll(base, -n, axis=0) * _POLARITY[n + 1], sections, "Data")
    return sections
