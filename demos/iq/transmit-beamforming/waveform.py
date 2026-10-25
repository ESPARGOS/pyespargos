"""Waveform editor defaults and preview recipes."""

from pathlib import Path
import numpy as np


def default_beam_settings():
    return dict(
        type="cw",
        amplitude=0.1,
        offset_mhz=1.0,
        first_mhz=-1.0,
        second_mhz=1.0,
        start_mhz=-40.0,
        stop_mhz=40.0,
        duration_us=0.4,
        delay_us=0.0,
        file="",
        u=0.0,
        v=0.0,
        squint=False,
    )


def load_iq(path, samples):
    """Read complex .npy or two-column I/Q text, normalize to signed 10-bit."""
    data = np.load(path, allow_pickle=False) if Path(path).suffix.lower() == ".npy" else np.loadtxt(path, delimiter="," if Path(path).suffix.lower() == ".csv" else None)
    if data.ndim == 2 and data.shape[1] == 2:
        data = data[:, 0] + 1j * data[:, 1]
    if data.ndim != 1 or not np.iscomplexobj(data) or len(data) != samples or not np.isfinite(data).all():
        raise ValueError(f"Expected {samples} finite complex samples or {samples} rows of I,Q")
    peak = max(abs(data.real).max(), abs(data.imag).max())
    if peak == 0:
        return np.zeros(samples, dtype="<u4")
    i = np.rint(data.real * 511 / peak).astype(np.int32)
    q = np.rint(data.imag * 511 / peak).astype(np.int32)
    return ((i & 1023) | ((q & 1023) << 10)).astype("<u4")


def synthesized_waveform(beam):
    from espargos.waveforms import CW, TwoTone, LFM, Noise, QPSK

    kind = beam["type"]
    if kind == "cw":
        return CW(offset_hz=beam["offset_mhz"] * 1e6)
    if kind == "two-tone":
        return TwoTone(first_hz=beam["first_mhz"] * 1e6, second_hz=beam["second_mhz"] * 1e6)
    if kind == "lfm":
        return LFM(start_hz=beam["start_mhz"] * 1e6, stop_hz=beam["stop_mhz"] * 1e6, duration_s=beam["duration_us"] * 1e-6, delay_s=beam["delay_us"] * 1e-6)
    return {"noise": Noise, "qpsk": QPSK}[kind]()


def waveform(beam, samples):
    recipe = dict(type="file", samples=samples) if beam["type"] == "file" else synthesized_waveform(beam).recipe(samples)
    return {**recipe, "amplitude": beam["amplitude"]}
