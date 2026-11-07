#!/usr/bin/env python3
"""Pairwise center-frequency phase, overlapping ADEV and sampled phase PSD."""

import argparse
import csv
import itertools
import json
import os
import pathlib

os.environ.setdefault("MPLCONFIGDIR", "/tmp/espargos-matplotlib")
import numpy as np
from scipy import signal
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def analyze(path):
    meta = json.loads(path.with_suffix(".json").read_text())
    d = np.load(path)
    z = d["csi"]
    if len(z) < 100:
        return {"file": str(path), "error": "Too few complete packets", "n": len(z)}
    if np.any(np.diff(d["t"]) < -1):
        result = {"file": str(path), "error": "Timestamp reset: mixed clock epochs; excluded", "n": len(z)}
        pathlib.Path(str(path.with_suffix("")) + "_analysis.json").write_text(json.dumps(result, indent=2))
        return result
    order = np.argsort(d["t"])
    t = d["t"][order]
    z = z[order]
    t = t - t[0]
    fmt = meta["format"]
    k = np.arange(z.shape[-1]) - z.shape[-1] // 2
    mask = (abs(k) >= 2) & (abs(k) <= (120 if fmt == "he20" else 24))
    if fmt == "lltf12":
        mask &= k % 2 == 0
    k = k[mask].astype(float)
    z = z[:, :, mask].astype(np.complex128)
    receivers = [i for i in range(8) if i != meta["tx"]]
    pairs = list(itertools.combinations(receivers, 2))
    phases = []
    slopes = []
    residual = []
    for i, j in pairs:
        q = z[:, i] * z[:, j].conj()
        q /= np.maximum(abs(q), 1e-30)
        template = np.mean(q, axis=0)
        angle = np.angle(q * template.conj())
        # Fixed weights; excludes unreliable faded tones without time-varying weighting.
        power_i = np.median(abs(z[:, i]) ** 2, axis=0)
        power_j = np.median(abs(z[:, j]) ** 2, axis=0)
        w = power_i * power_j / np.maximum(power_i + power_j, 1e-30)
        x = np.column_stack([np.ones(len(k)), k])
        inverse = np.linalg.pinv(x * np.sqrt(w)[:, None])
        # Circular regression avoids propagating a phase-wrap error at one
        # faded tone through the whole band. Fit intercept AND delay slope.
        fit = np.zeros((len(angle), 2))
        fit[:, 0] = np.angle(np.sum(w * np.exp(1j * angle), axis=1))
        for _ in range(8):
            err = np.angle(np.exp(1j * (angle - fit @ x.T)))
            fit += (inverse @ (err * np.sqrt(w)).T).T
        phase = np.unwrap(fit[:, 0])
        phases.append(phase - phase.mean())
        slopes.append(fit[:, 1])
        residual.append(np.sqrt(np.mean(np.angle(np.exp(1j * (angle - fit @ x.T))) ** 2)))
    phase = np.array(phases).T
    dt = 0.01
    # Remove the arbitrary first-packet contention delay before grid assignment.
    offset = np.angle(np.mean(np.exp(2j * np.pi * t / dt))) * dt / (2 * np.pi)
    slots = np.rint((t - offset) / dt).astype(int)
    slots -= slots.min()
    timing_error = t - slots * dt - offset
    # Use actual timestamps to validate a regular schedule; never fill missing samples.
    grid = np.full((slots[-1] + 1, len(pairs)), np.nan)
    good = abs(timing_error) < 0.0005
    unique, multiplicity = np.unique(slots, return_counts=True)
    good &= ~np.isin(slots, unique[multiplicity > 1])
    grid[slots[good]] = phase[good]
    fc = (2407 + 5 * meta["settings"]["wifi"]["channel-primary"]) * 1e6
    taus = []
    adev = []
    counts = []
    for m in [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000]:
        if 2 * m >= len(grid) or m * dt > t[-1] / 10:
            continue
        diff = grid[2 * m :] - 2 * grid[m:-m] + grid[: -2 * m]
        count = np.sum(np.isfinite(diff), axis=0)
        taus.append(m * dt)
        adev.append(np.sqrt(np.nanmean(diff**2, axis=0) / 2) / (2 * np.pi * fc * m * dt))
        counts.append(count)
    # Short complete windows survive periodic packet loss. No interpolation;
    # resolution is 3.125 Hz, so do not claim a 1 Hz phase-noise measurement.
    nperseg = 32
    spectra = []
    for start in range(0, len(grid) - nperseg + 1, nperseg // 2):
        block = grid[start : start + nperseg]
        if np.all(np.isfinite(block)):
            f, ps = signal.periodogram(block, fs=1 / dt, window="hann", detrend="linear", axis=0)
            spectra.append(ps)
    if spectra:
        psd = np.mean(spectra, axis=0)
    else:
        f = np.array([])
        psd = np.empty((0, len(pairs)))
    rms = np.std(phase, axis=0) * 180 / np.pi
    trend = np.polyfit(t, phase, 1)
    detrended = phase - (t[:, None] * trend[0] + trend[1])
    rxgain = d["rx_gain"][:, receivers]
    fftgain = d["fft_gain"][:, receivers]
    result = dict(
        file=str(path),
        host=meta["host"],
        format=fmt,
        tx=meta["tx"],
        n=len(t),
        elapsed_s=t[-1],
        complete_fraction=len(np.unique(slots)) / len(grid),
        timing_accepted_fraction=float(np.mean(good)),
        timing_error_max_us=float(np.max(abs(timing_error)) * 1e6),
        duplicate_slots=len(slots) - len(np.unique(slots)),
        rssi_median_dbm=np.median(d["rssi"][:, receivers], axis=0).tolist(),
        rx_gain_unique=[np.unique(rxgain[:, i]).tolist() for i in range(7)],
        fft_gain_unique=[np.unique(fftgain[:, i]).tolist() for i in range(7)],
        pair_labels=[f"{i}-{j}" for i, j in pairs],
        phase_rms_deg=rms.tolist(),
        detrended_rms_deg=(np.std(detrended, axis=0) * 180 / np.pi).tolist(),
        drift_deg_per_min=(trend[0] * 180 / np.pi * 60).tolist(),
        phase_p95_abs_deg=np.percentile(abs(phase) * 180 / np.pi, 95, axis=0).tolist(),
        spectral_fit_residual_deg=(np.array(residual) * 180 / np.pi).tolist(),
        taus_s=taus,
        adev=np.array(adev).tolist(),
        adev_counts=np.array(counts).tolist(),
        allan_variance=(np.array(adev) ** 2).tolist(),
        equivalent_time_rms_ps=(rms / 360 / fc * 1e12).tolist(),
        psd_windows=len(spectra),
        phase_noise_dbc_hz={str(hz): (10 * np.log10(np.mean(psd[(f >= hz * 0.8) & (f <= hz * 1.2)], axis=0) / 2)).tolist() for hz in [10, 30] if len(f)},
        integrated_phase_rms_3p125_40_hz_deg=(np.sqrt(np.sum(psd[(f >= 3.125) & (f <= 40)], axis=0) * (100 / nperseg)) * 180 / np.pi).tolist() if len(f) else [],
        phase_step_gt_30deg=int(np.sum(abs(np.diff(phase, axis=0)) > np.pi / 6)),
    )
    result["phase_change_rms_deg"] = {}
    for lag_s in [0.01, 0.1, 1, 10]:
        m = round(lag_s / dt)
        if len(grid) > m:
            result["phase_change_rms_deg"][str(lag_s)] = (np.sqrt(np.nanmean((grid[m:] - grid[:-m]) ** 2, axis=0)) * 180 / np.pi).tolist()
    if "clip_fraction" in d:
        result["raw_near_limit_fraction"] = np.nanmean(d["clip_fraction"][:, receivers], axis=0).tolist()
        result["raw_peak_max"] = np.nanmax(d["raw_peak"][:, receivers], axis=0).tolist()
    stem = path.with_suffix("")
    pathlib.Path(str(stem) + "_analysis.json").write_text(json.dumps(result, indent=2))
    with pathlib.Path(str(stem) + "_pairs.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(["pair", "phase_rms_deg", "detrended_rms_deg", "drift_deg_per_min", "equivalent_time_rms_ps"])
        writer.writerows(zip(result["pair_labels"], rms, result["detrended_rms_deg"], result["drift_deg_per_min"], result["equivalent_time_rms_ps"]))
    np.savez_compressed(str(stem) + "_analysis.npz", t=t, phase=phase, frequency=f, psd=psd, taus=taus, adev=adev, pairs=pairs, slopes=slopes)
    fig, axs = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    for index in range(len(pairs)):
        axs[0, 0].plot(t[:: max(1, len(t) // 4000)], phase[:: max(1, len(t) // 4000), index] * 180 / np.pi, lw=0.5)
    axs[0, 0].set(xlabel="Time (s)", ylabel="Relative phase (degrees)", title="21 RX pairs; mean removed")
    axs[0, 1].bar(range(len(pairs)), rms)
    axs[0, 1].set_xticks(range(len(pairs)), result["pair_labels"], rotation=90)
    axs[0, 1].set(ylabel="Phase RMS (degrees)", xlabel="Row-major sensor pair")
    if len(f):
        axs[1, 0].semilogx(f[1:], 10 * np.log10(psd[1:] / 2), lw=0.6)
    axs[1, 0].set(xlabel="Offset frequency (Hz)", ylabel="Equivalent SSB phase noise (dBc/Hz)", title=f"Sampled CSI; {len(spectra)} complete Welch windows")
    axs[1, 1].loglog(taus, adev, lw=0.8)
    axs[1, 1].set(xlabel="Averaging time (s)", ylabel="Fractional frequency Allan deviation")
    for ax in axs.flat:
        ax.grid(alpha=0.2)
    fig.suptitle(f'{meta["host"]}: {fmt}, TX {meta["tx"]}, {len(t)} packets / {t[-1]:.1f} s')
    fig.savefig(str(stem) + "_analysis.png", dpi=160)
    plt.close(fig)
    return {k: result[k] for k in ["host", "format", "tx", "n", "elapsed_s", "complete_fraction", "timing_error_max_us", "psd_windows", "rssi_median_dbm", "phase_step_gt_30deg"]} | dict(
        rms_median=float(np.median(rms)), rms_max=float(max(rms)), rms_min=float(min(rms))
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="+")
    a = p.parse_args()
    for item in a.paths:
        print(json.dumps(analyze(pathlib.Path(item))), flush=True)
