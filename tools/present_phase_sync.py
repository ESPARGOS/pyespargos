#!/usr/bin/env python3
"""Robust phase stability presentation with untrimmed results in an appendix."""

import json
import os
import pathlib

os.environ.setdefault("MPLCONFIGDIR", "/tmp/espargos-matplotlib")
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

root = pathlib.Path(__file__).resolve().parents[1] / "measurements/phase_sync"
hosts = ["192.168.1.2", "172.16.0.241"]
lag_values = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30, 60]
summary = {}


def central_stats(values):
    """Per-pair empirical coverage and explicitly trimmed RMS, in degrees."""
    median = np.median(values, axis=0)
    lo, hi = np.percentile(values, [2.5, 97.5], axis=0)
    retained = (values >= lo) & (values <= hi)
    trimmed_rms = np.sqrt(np.sum(np.where(retained, values**2, 0), axis=0) / retained.sum(axis=0))
    mad_sigma = 1.482602218505602 * np.median(abs(values - median), axis=0)
    return dict(
        median_deg=median.tolist(),
        lower_95_deg=lo.tolist(),
        upper_95_deg=hi.tolist(),
        p95_abs_deg=np.percentile(abs(values), 95, axis=0).tolist(),
        central_95_rms_deg=trimmed_rms.tolist(),
        mad_sigma_deg=mad_sigma.tolist(),
        retained_fraction=np.mean(retained, axis=0).tolist(),
    )


fig, axs = plt.subplots(1, 2, figsize=(12, 4.8), constrained_layout=True)
for host, color in zip(hosts, ["tab:blue", "tab:orange"]):
    base = root / host / "exclusive_repeat"
    d = np.load(base / "lltf12_tx2_analysis.npz")
    a = json.loads((base / "lltf12_tx2_analysis.json").read_text())
    t = d["t"]
    phase = d["phase"]
    out = {"observed_seconds": float(t[-1]), "packets": len(t), "lags": {}, "median_pair_rms_deg": float(np.median(a["phase_rms_deg"])), "worst_pair_rms_deg": float(max(a["phase_rms_deg"]))}
    phase_deg = phase * 180 / np.pi
    out["robust_phase"] = central_stats(phase_deg - np.median(phase_deg, axis=0))
    for tau in lag_values:
        if tau > t[-1] / 4:
            continue
        target = t + tau
        right = np.searchsorted(t, target)
        right = np.clip(right, 1, len(t) - 1)
        left = right - 1
        j = np.where(abs(t[right] - target) < abs(t[left] - target), right, left)
        keep = (abs(t[j] - target) <= 0.005) & (j > np.arange(len(t)))
        delta = (phase[j[keep]] - phase[keep]) * 180 / np.pi
        rms = np.sqrt(np.mean(delta**2, axis=0))
        p99 = np.percentile(abs(delta), 99, axis=0)
        out["lags"][str(tau)] = {
            "matched_endpoint_pairs": int(np.sum(keep)),
            "robust": central_stats(delta),
            "rms_deg": rms.tolist(),
            "p99_abs_deg": p99.tolist(),
            "median_pair_rms_deg": float(np.median(rms)),
            "worst_pair_rms_deg": float(max(rms)),
            "worst_pair_p99_deg": float(max(p99)),
        }
    coherence = abs(np.mean(np.exp(1j * phase), axis=0))
    worst_per_packet = np.max(abs(phase) * 180 / np.pi, axis=1)
    out["pair_coherence"] = coherence.tolist()
    out["median_pair_coherence"] = float(np.median(coherence))
    out["worst_pair_coherence"] = float(min(coherence))
    out["worst_absolute_phase_deg"] = float(max(worst_per_packet))
    out["packet_fraction_any_pair_gt_deg"] = {str(th): float(np.mean(worst_per_packet > th)) for th in [1, 2, 5, 10, 20, 30]}
    out["packet_count_any_pair_gt_deg"] = {str(th): int(np.sum(worst_per_packet > th)) for th in [1, 2, 5, 10, 20, 30]}
    out["worst_per_packet_p99_deg"] = float(np.percentile(worst_per_packet, 99))
    summary[host] = out
    tau = np.array([float(x) for x in out["lags"]])
    rms = np.array([x["rms_deg"] for x in out["lags"].values()])
    axs[0].semilogx(tau, np.median(rms, axis=1), "o-", color=color, label=host)
    axs[0].fill_between(tau, rms.min(axis=1), rms.max(axis=1), color=color, alpha=0.15)
    thresholds = np.geomspace(0.1, max(50, float(max(worst_per_packet)) * 1.1), 250)
    survival = np.array([np.mean(worst_per_packet > x) for x in thresholds])
    nz = survival > 0
    axs[1].loglog(thresholds[nz], survival[nz] * 100, color=color, label=host)
axs[0].set(xlabel="Elapsed time between observations (s)", ylabel="Differential phase change RMS (degrees)", title="Phase stability versus elapsed time\nLine: median RX pair; band: best to worst pair")
axs[1].set(xlabel="Phase-error threshold (degrees)", ylabel="Packets exceeding threshold (%)", title="Tail behavior across all 21 RX pairs\nAny pair, deviation from its full-record mean")
for ax in axs:
    ax.grid(which="both", alpha=0.25)
    ax.legend()
fig.suptitle("Repeat after other controllers were closed — 12-bit L-LTF, CFO off, frozen gains")
fig.savefig(root / "stability_presentation_untrimmed.png", dpi=180)
plt.close(fig)
(root / "stability_presentation.json").write_text(json.dumps(summary, indent=2))
lines = [
    "# How to present and judge the synchronization",
    "",
    "Use **differential phase change versus elapsed time, in degrees**, as the primary stability measure. Accompany it with a tail-error statistic and acquisition availability. For an antenna array, this directly answers how reliably receiver phases retain their relationship, and how much a stale phase calibration can change. A small Allan deviation alone does not answer those questions.",
    "",
    "![Phase stability versus elapsed time and outlier distribution](stability_presentation_untrimmed.png)",
    "",
    "The left plot uses `sqrt(mean((phi_ij(t+tau)-phi_ij(t))^2))`. It compares actual received endpoint samples with lag within ±5 ms of the stated tau; no interpolation is used and no phase outliers are rejected. The tolerance is relatively large at the 10 ms point. Values include noise in both endpoint packets; a calibration averaged over multiple packets can reduce the reference contribution. Long-lag points have limited independent intervals. The right plot retains every complete packet and shows the fraction on which at least one receiver pair exceeds a phase-error threshold, relative to its full-record mean. It is descriptive, not an independent calibration-accuracy test.",
    "",
    "## Recommended numerical summary",
    "",
    "Results below use the repeat performed after the user reported closing other controlling processes. Each record uses one transmitter and seven receivers (21 pairs). The earlier measurements cover the remaining receiver pairs.",
    "",
    "| Metric | 192.168.1.2 | 172.16.0.241 |",
    "|---|---:|---:|",
]


def row(label, values):
    lines.append("| " + label + " | " + " | ".join(values) + " |")


row("Observed span", [f'{summary[h]["observed_seconds"]:.2f} s' for h in hosts])
row("Median pair phase RMS over record", [f'{summary[h]["median_pair_rms_deg"]:.3f}°' for h in hosts])
row("Worst pair phase RMS over record", [f'{summary[h]["worst_pair_rms_deg"]:.3f}°' for h in hosts])
for tau in [0.01, 1.0, 10.0, 60.0]:
    key = str(tau) if tau < 1 else str(int(tau))
    if all(key in summary[h]["lags"] for h in hosts):
        row(f"Median pair RMS phase change over {tau:g} s", [f'{summary[h]["lags"][key]["median_pair_rms_deg"]:.3f}°' for h in hosts])
        if tau == 10:
            row("Worst-pair 99th percentile phase change over 10 s", [f'{summary[h]["lags"][key]["worst_pair_p99_deg"]:.2f}°' for h in hosts])
row("99th percentile of packet-wise worst pair error", [f'{summary[h]["worst_per_packet_p99_deg"]:.2f}°' for h in hosts])
row("Largest observed deviation from pair mean", [f'{summary[h]["worst_absolute_phase_deg"]:.2f}°' for h in hosts])
row("Packets with any pair error >10°", [f'{summary[h]["packet_count_any_pair_gt_deg"]["10"]} / {summary[h]["packets"]} ({100*summary[h]["packet_fraction_any_pair_gt_deg"]["10"]:.4f}%)' for h in hosts])
row("Median / worst-pair temporal coherence", [f'{100*summary[h]["median_pair_coherence"]:.5f}% / {100*summary[h]["worst_pair_coherence"]:.5f}%' for h in hosts])
lines += [
    "",
    "## Quality judgment",
    "",
    "**For ordinary coherent combining and beamforming, degree-level differential phase fluctuations are very good.** The measured temporal coherence above is `C_ij = |mean(exp(j*phi_ij))|`, the retention of pairwise correlation amplitude due to phase fluctuations alone. It is computed directly from the data, retaining outliers and without assuming Gaussian errors. It excludes amplitude errors, geometry errors, and fixed phase-calibration errors, so it is not the measured efficiency of a complete beamformer.",
    "",
    "For scale, Gaussian differential phase noise with 1° RMS gives `C = exp(-sigma_phi^2/2) = 0.999848`, or only 0.0152% loss of pairwise correlation amplitude. This relation and the pairwise convention are used in [NRAO ngVLA system requirements](https://ngvla.nrao.edu/download/MediaFile/277/original). Larger rare errors matter more for individual angle/radar estimates and deep spatial nulls than their small contribution to average coherence might suggest.",
    "",
    "**For precision phase metrology or a hard maximum-error requirement, these results are not a certification.** Static phase alignment was removed, environmental stationarity was not established, packet-based CSI adds estimator/quantization noise, and very rare events cannot be bounded by a five-minute test. Sub-degree absolute direction finding, very deep nulling, and long unattended operation require separate calibration-accuracy and reliability validation.",
    "",
    "Present **Allan deviation** as a supporting clock/frequency-stability diagnostic, with carrier frequency, sampling, and gap policy stated. ADEV is easier to read than Allan variance because it retains fractional-frequency rather than squared units. White or flicker phase noise can make ADEV decrease approximately as `1/tau` even when the phase-error scale does not improve; ordinary ADEV alone does not separate those two noise types. See [NIST on Allan and modified Allan variance](https://www.nist.gov/system/files/documents/calibrations/pfc-2.pdf). Thus values around 10^-12 at one second must not be presented as evidence of 10^-12-radian phase accuracy.",
    "",
    "Keep the **sampled phase-noise spectrum** as the diagnostic for broadband noise and spurs, specifying the 100 Hz packet rate, 3.125 Hz spectral resolution, complete-window selection, and 50 Hz Nyquist limit. Do not label it an intrinsic RF oscillator phase-noise specification.",
    "",
    "**Acquisition reliability improved in this repeat:** both arrays completed five minutes, without a sensor uptime reset or backwards timestamp warning, with 97.28% and 98.16% complete-packet fractions. Start/end settings matched, per-packet gains remained fixed, and no digital clipping-limit values were observed. However, the TX sensor still logged five and twelve in-place CSI watchdog reinitializations, respectively. This suggests a TX/watchdog interaction worth investigating before unattended operation; it is not evidence of receiver phase-lock failure. The [repeat report](EXCLUSIVE_REPEAT.md) contains the counters and restoration checks.",
    "",
    "My overall assessment is **very good average within-array coherence, but not uniformly sub-degree phase precision and not yet a demonstrated long-term reliability guarantee**. Typical phase evolution is small; the roughly 1% of packets with at least one pair more than 10° from its mean makes the error tails the main concern for precision single-packet applications. That tail includes propagation and CSI estimator behavior, so it cannot all be attributed to synchronization hardware. All conclusions are within-array; no between-array synchronization was measured.",
    "",
]
(root / "PRESENTATION_UNTRIMMED.md").write_text("Appendix: untrimmed statistics, retained for traceability. The [main presentation](PRESENTATION.md) uses robust central-distribution measures.\n\n" + "\n".join(lines))

fig, ax = plt.subplots(figsize=(8.5, 5), constrained_layout=True)
for host, color in zip(hosts, ["tab:blue", "tab:orange"]):
    tau = np.array([float(x) for x in summary[host]["lags"]])
    coverage = np.array([x["robust"]["p95_abs_deg"] for x in summary[host]["lags"].values()])
    ax.semilogx(tau, np.median(coverage, axis=1), "o-", color=color, label=host)
    ax.fill_between(tau, coverage.min(axis=1), coverage.max(axis=1), color=color, alpha=0.15)
ax.set(xlabel="Elapsed time between observations (s)", ylabel="95th percentile of absolute phase change (degrees)", title="Typical within-array phase stability\nLine: median of 21 RX pairs; band: pair-to-pair range")
ax.grid(which="both", alpha=0.25)
ax.legend()
fig.savefig(root / "stability_presentation.png", dpi=180)
plt.close(fig)

lines = [
    "# Typical within-array phase stability",
    "",
    "The headline result is the **95th percentile of absolute differential phase change versus elapsed time**. This describes the usual phase stability while reducing the influence of a small number of extreme samples. Each curve summarizes 21 simultaneous RX pairs from a five-minute L-LTF capture with CFO correction off and frozen RX/FFT gains.",
    "",
    "![Typical phase stability, using 95% coverage](stability_presentation.png)",
    "",
    "The line is the median of the 21 per-pair 95th percentiles; the shaded band is their minimum-to-maximum range. It is a description of a typical pair and pair-to-pair variation, not a simultaneous 95% bound for the entire array. These are empirical **coverage statistics**, not 95% confidence intervals on an estimated parameter. No Gaussian distribution is assumed for the coverage calculation.",
    "",
    "## Robust numerical summary",
    "",
    "Phase offsets over the record are referenced to each pair’s time median, removing the static offset. Summaries below are medians across the 21 pairs.",
    "",
    "| Metric | 192.168.1.2 | 172.16.0.241 |",
    "|---|---:|---:|",
]
row("Robust phase spread: Gaussian-scaled MAD", [f'{np.median(summary[h]["robust_phase"]["mad_sigma_deg"]):.3f}°' for h in hosts])
row("RMS of the central 95% of phase offsets", [f'{np.median(summary[h]["robust_phase"]["central_95_rms_deg"]):.3f}°' for h in hosts])
row("95% absolute phase-offset bound", [f'±{np.median(summary[h]["robust_phase"]["p95_abs_deg"]):.2f}°' for h in hosts])
for tau in [0.01, 1, 10, 60]:
    key = str(tau)
    if all(key in summary[h]["lags"] for h in hosts):
        row(f"95% absolute phase-change bound over {tau:g} s", [f'±{np.median(summary[h]["lags"][key]["robust"]["p95_abs_deg"]):.2f}°' for h in hosts])
row("Untrimmed median temporal coherence", [f'{100*summary[h]["median_pair_coherence"]:.4f}%' for h in hosts])
lines += [
    "",
    "The **scaled median absolute deviation (MAD)** is `1.4826 * median(abs(phi - median(phi)))`. The scale makes it comparable to standard deviation for Gaussian noise, while limiting sensitivity to extremes. It is a robust spread estimate, not a confidence interval.",
    "",
    "The **central-95% RMS** explicitly discards the lowest 2.5% and highest 2.5% of signed phase offsets for each pair, then calculates RMS about the original median reference. It is a conditional, trimmed statistic; it is smaller than a full-distribution RMS even for purely Gaussian noise. The 95% bounds are percentiles calculated from all samples, so their values do not depend on deciding which samples were interference.",
    "",
    "## Quality judgment",
    "",
    "**Typical synchronization is very good for coherent array processing:** the robust phase spread is approximately one degree, and the usual phase change remains small over the measured timescales. Even without excluding samples, median pairwise temporal coherence is above 99.96% on both arrays. This implies little average decorrelation from the measured phase fluctuations. It does not include amplitude errors or fixed phase-calibration errors.",
    "",
    "For perspective, 1° RMS Gaussian differential phase noise retains approximately 99.985% of correlation amplitude under `C = exp(-sigma_phi^2/2)` ([NRAO convention](https://ngvla.nrao.edu/download/MediaFile/277/original)). Both arrays completed the full five-minute repeat. The appropriate characterization is degree-level typical relative-phase stability with excellent average coherence, rather than absolute phase accuracy or an intrinsic oscillator-noise specification.",
    "",
    "## Supporting diagnostics and method",
    "",
    "Keep Allan deviation and sampled phase-noise spectra as secondary diagnostics in the [measurement report](EXCLUSIVE_REPEAT.md). Their existing values are not silently recalculated from a concatenated, outlier-deleted series: doing that would alter the timing and spectral meaning. Ordinary Allan deviation can fall approximately as `1/tau` for phase noise without the phase-error magnitude improving ([NIST](https://www.nist.gov/system/files/documents/calibrations/pfc-2.pdf)).",
    "",
    "Phase-change samples use actual endpoint timestamps within ±5 ms of the indicated lag, with no interpolation. The 10 ms point consequently has a relatively broad lag tolerance. Differences include noise at both endpoints; averaging a phase-calibration reference can reduce its contribution. Long-lag estimates have fewer independent observation intervals.",
    "",
    "The trimmed subset is selected separately for each receiver pair. No common set of physical packets is deleted, and raw data is preserved. Interference is a plausible source of unusual samples, but this analysis does not establish their cause. The [untrimmed appendix](PRESENTATION_UNTRIMMED.md) and [acquisition diagnostics](EXCLUSIVE_REPEAT.md) retain the complete record without making it the headline.",
    "",
]
if (root / "conference/README.md").exists():
    lines[2:2] = ["**Conference presentation:** use the [literature-standard Allan-deviation and phase-deviation figures](conference/README.md). The percentile-based presentation below is retained as a descriptive supplement.", ""]
(root / "PRESENTATION.md").write_text("\n".join(lines))
print(
    json.dumps(
        {
            h: {
                "mad_sigma_median_deg": float(np.median(d["robust_phase"]["mad_sigma_deg"])),
                "central_95_rms_median_deg": float(np.median(d["robust_phase"]["central_95_rms_deg"])),
                "p95_abs_phase_median_deg": float(np.median(d["robust_phase"]["p95_abs_deg"])),
                "p95_abs_change_median_deg": {tau: float(np.median(x["robust"]["p95_abs_deg"])) for tau, x in d["lags"].items()},
            }
            for h, d in summary.items()
        },
        indent=2,
    )
)
