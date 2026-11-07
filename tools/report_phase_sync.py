#!/usr/bin/env python3
"""Assemble the captured phase stability results without selecting quiet data."""

import csv
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
primary = ["long_retry/lltf12_tx2", "long/lltf12_tx2"]
main = []
all_records = []
for host in hosts:
    for p in sorted((root / host).glob("*/*_analysis.json")):
        if p.parent.name == "exclusive_repeat":
            continue  # Follow-up has its own report; preserve the original experiment.
        d = json.loads(p.read_text())
        if "error" not in d:
            d["_relative"] = str(p.relative_to(root)).replace("_analysis.json", "")
            all_records.append(d)
fig, axs = plt.subplots(2, 2, figsize=(12, 10), constrained_layout=True)
coverage = []
for row, (host, record) in enumerate(zip(hosts, primary)):
    d = json.loads((root / host / (record + "_analysis.json")).read_text())
    main.append(d)
    candidates = [x for x in all_records if x["host"] == host and x["format"] == "lltf12" and ("/long" in x["_relative"] or "/tx5/" in x["_relative"] or "/tx0/" in x["_relative"])]
    matrix = np.full((8, 8), np.nan)
    for i in range(8):
        for j in range(i + 1, 8):
            label = f"{i}-{j}"
            possible = [x for x in candidates if label in x["pair_labels"]]
            if not possible:
                continue
            chosen = max(possible, key=lambda x: x["elapsed_s"])
            value = chosen["phase_rms_deg"][chosen["pair_labels"].index(label)]
            matrix[i, j] = matrix[j, i] = value
            coverage.append([host, label, value, chosen["elapsed_s"], chosen["_relative"]])
    im = axs[row, 0].imshow(matrix, vmin=0, vmax=3, cmap="viridis")
    for i in range(8):
        for j in range(8):
            if np.isfinite(matrix[i, j]):
                axs[row, 0].text(j, i, f"{matrix[i,j]:.2f}", ha="center", va="center", color="white", fontsize=8)
    axs[row, 0].set(title=f"{host}: pairwise phase RMS (degrees)", xlabel="Row-major receiver index", ylabel="Row-major receiver index", xticks=range(8), yticks=range(8))
    fig.colorbar(im, ax=axs[row, 0], shrink=0.8)
    a = np.array(d["adev"])
    tau = np.array(d["taus_s"])
    axs[row, 1].loglog(tau, np.median(a, axis=1), "o-", label="Median of 21 RX pairs")
    axs[row, 1].fill_between(tau, a.min(axis=1), a.max(axis=1), alpha=0.2, label="Min–max across RX pairs")
    axs[row, 1].set(title=f'L-LTF, TX 2, {d["elapsed_s"]:.1f} s observed', xlabel="Averaging time (s)", ylabel="Fractional-frequency Allan deviation")
    axs[row, 1].grid(which="both", alpha=0.2)
    axs[row, 1].legend(fontsize=8)
fig.suptitle("Within-array stability: CFO correction off, frozen gains, 8.5 dBm TX\nHeatmaps use the longest L-LTF record available for each pair; durations differ")
fig.savefig(root / "overview.png", dpi=180)
plt.close(fig)
with (root / "all_pairs.csv").open("w") as stream:
    writer = csv.writer(stream)
    writer.writerow(["host", "pair", "rms_deg", "observed_seconds", "source_record"])
    writer.writerows(coverage)

lines = [
    "# ESPARGOS within-array phase stability — 18 September 2026",
    "",
    "The measured differential receiver phase is typically stable to about one degree RMS in the L-LTF experiment. This is an over-the-air, end-to-end CSI result; it includes propagation and estimation noise and does not isolate intrinsic PLL noise. Static pair offsets have been removed. The two arrays were never treated as synchronized to one another.",
    "",
    "## Main L-LTF result",
    "",
    "CFO correction forced to zero; analog RX gain and FFT scale frozen per receiver; uncompressed, forced 12-bit L-LTF; one right-port radar transmitter at 8.5 dBm; 100 packets/s. Channels were 8 (2447 MHz) and 5 (2432 MHz), respectively. TX sensor 2 leaves seven receivers and 21 simultaneous receiver pairs.",
    "",
    "| Quantity | 192.168.1.2 | 172.16.0.241 |",
    "|---|---:|---:|",
]
rows = [
    ("Requested capture", "300 s", "300 s"),
    ("Observed uninterrupted span", *[f'{x["elapsed_s"]:.2f} s' for x in main]),
    ("Complete packets", *[str(x["n"]) for x in main]),
    ("Complete fraction within observed span", *[f'{100*x["complete_fraction"]:.2f}%' for x in main]),
    ("Pair phase RMS: median", *[f'{np.median(x["phase_rms_deg"]):.3f}°' for x in main]),
    ("Pair phase RMS: min–max", *[f'{min(x["phase_rms_deg"]):.3f}–{max(x["phase_rms_deg"]):.3f}°' for x in main]),
    ("Equivalent RF time RMS: median", *[f'{np.median(x["equivalent_time_rms_ps"]):.3f} ps' for x in main]),
    ("Largest absolute linear phase drift", *[f'{max(abs(np.array(x["drift_deg_per_min"]))):.3f}°/min' for x in main]),
    ("Equivalent SSB noise near 10 Hz: median", *[f'{np.median(x["phase_noise_dbc_hz"]["10"]):.2f} dBc/Hz' for x in main]),
    ("Equivalent SSB noise near 30 Hz: median", *[f'{np.median(x["phase_noise_dbc_hz"]["30"]):.2f} dBc/Hz' for x in main]),
    ("Integrated 3.125–40 Hz phase RMS: median", *[f'{np.median(x["integrated_phase_rms_3p125_40_hz_deg"]):.3f}°' for x in main]),
]
for row in rows:
    lines.append("| " + " | ".join(row) + " |")
lines += [
    "",
    "## Allan stability",
    "",
    "Values below are medians across receiver pairs. Allan deviation is fractional-frequency stability derived from differential phase at the RF carrier, not an Allan statistic applied directly to phase. The variance is its square.",
    "",
    "| Averaging time | ADEV: 192.168.1.2 | ADEV: 172.16.0.241 | AVAR: 192.168.1.2 | AVAR: 172.16.0.241 |",
    "|---|---:|---:|---:|---:|",
]
for tau in [0.01, 0.1, 1, 10]:
    vals = [np.median(x["adev"][x["taus_s"].index(tau)]) for x in main]
    lines.append(f"| {tau:g} s | {vals[0]:.3g} | {vals[1]:.3g} | {vals[0]**2:.3g} | {vals[1]**2:.3g} |")
lines += [
    "",
    "The approximately inverse-tau dependence and fairly flat sampled phase spectrum are consistent with a substantial white-phase/measurement-noise contribution. They do not identify the physical noise source. Long-tau estimates have limited independent observations; saved overlapping term counts are not confidence-interval degrees of freedom.",
    "",
    "## Coverage and waveform comparisons",
    "",
    "Transmitters 2, 5, and 0 collectively allow all 28 pairs of the eight receivers to be measured. Indices are zero-based row-major: top row 0–3, bottom row 4–7. The heatmap uses the longest available 8.5 dBm L-LTF record containing each pair, not the quietest record. It combines different times, transmitters, and observation lengths; it is not a simultaneous eight-receiver measurement. [Per-pair values and source records](all_pairs.csv).",
    "",
    "![Pairwise phase RMS and Allan deviation](overview.png)",
    "",
    "| Array | Run | Format | TX | Observed seconds | Packets | Median pair RMS | Worst pair RMS |",
    "|---|---|---|---:|---:|---:|---:|---:|",
]
for x in all_records:
    run = x["_relative"].split("/")[1]
    lines.append(f'| {x["host"]} | {run} | {x["format"]} | {x["tx"]} | {x["elapsed_s"]:.2f} | {x["n"]} | {np.median(x["phase_rms_deg"]):.3f}° | {max(x["phase_rms_deg"]):.3f}° |')
lines += [
    "",
    "The initial `pilot`/`pilot_repeat` records used 2 dBm; subsequent records used 8.5 dBm. L-LTF and HE20 were both usable. HE20 did not consistently improve the measured stability. These sequential runs do not hold the propagation environment constant, so differences are not a clean causal ranking of training fields. I would use the tested 12-bit L-LTF configuration as the baseline here. RX and FFT gains were verified constant within the selected records. Later records also save digital near-limit counts and peaks; these cannot rule out analog clipping.",
    "",
    "## Interruptions and limits",
    "",
    "192.168.1.2 exhibited two backwards TX/sensor timestamp jumps. Those mixed-epoch records (`pilot/lltf8_tx2` and `long/lltf12_tx2`) are excluded. Its retry produced 172.44 seconds of valid data, then no further complete clusters during the remainder of the requested 300 seconds. The TX-5 L-LTF run also ceased producing complete clusters before its requested end. Availability is therefore worse than the conditional packet-completion percentages in the table suggest.",
    "",
    "A read-only sensor-debug snapshot found two CSI watchdog reinitializations on firmware antenna ID 6 (row-major sensor 2, ESP 5), and a shorter uptime than the other sensors. Local firmware contains a CSI watchdog that reinitializes a stalled CSI engine. This supports a firmware/acquisition concern, but does not establish the exact cause of every reset or stall. See [saved debug snapshot](192.168.1.2/debug_during_tx5.json). No firmware was changed.",
    "",
    "Final debug readbacks also contain watchdog history on 172.16.0.241 and three reinitializations on antenna ID 1 (row-major sensor 5) of 192.168.1.2. Without initial counter baselines, not every historical counter can be attributed to this experiment. Both arrays therefore deserve acquisition-reliability follow-up; the counters alone do not quantify phase stability.",
    "",
    "The 172.16.0.241 TX-2 L-LTF record completed 300 seconds and its HE20 record completed 180 seconds. Its long L-LTF trace contains three brief excursions near 220.84, 254.56, and 295.93 seconds, reaching 36.0 degrees from the pair mean (six entering/leaving transitions exceeding 30 degrees across at least one pair). The worst pair has a 99th-percentile absolute error of 7.70 degrees. By comparison, the 192.168.1.2 main record reaches 6.17 degrees at its largest deviation, and its worst pair has a 99th-percentile error of 2.57 degrees. These are not maximum-error guarantees. The excursions are retained in the overall RMS. Their correlation with irregular packet timing means the near-uniform-sampling filter can exclude them from PSD/Allan estimates.",
    "",
    "Environmental stationarity was not independently established. Treat these results as observed receive-chain stability, not a guaranteed intrinsic hardware specification or absolute phase calibration.",
    "",
    "Phase-noise numbers are approximate **sampled-CSI equivalent SSB densities**, not a continuous RF oscillator phase-noise specification. Only complete, near-uniform 0.32-second windows enter the spectrum. Resolution is 3.125 Hz, Hann noise bandwidth is 4.6875 Hz, and Nyquist is 50 Hz. The “10 Hz” value is the 9.375 Hz bin. Missing/delayed packets can bias which intervals enter the spectrum. All packets within an accepted clock epoch enter the overall phase RMS; no quiet subset was selected for it.",
    "",
    "## Files and reproducibility",
    "",
    "[Detailed method, equations, and limitations](METHOD.md). Each run directory contains the full complex CSI and hardware timestamps (`.npz`), receiver/TX settings (`.json`), per-pair CSV, analysis arrays, and a four-panel plot. The original settings and restoration status are also retained. Final board readbacks are in each array’s `final_status.json`; an empty `restoration_differences` object confirms restoration.",
    "",
    "Reproduce capture with `tools/measure_phase_sync.py`; analyze with `tools/analyze_phase_sync.py`; regenerate this report with `tools/report_phase_sync.py`. `tools/validate_phase_sync_analysis.py` passed a synthetic test of differential phase extraction, transmitter common-mode rejection, timing-slope rejection, Allan normalization, and SSB PSD scaling.",
    "",
    "Definitions: [NIST SP 1065](https://www.nist.gov/publications/handbook-frequency-stability-analysis), [NIST phase/frequency spectral relationships](https://tf.nist.gov/phase/Properties/eight.htm).",
    "",
]
if (root / "PRESENTATION.md").exists():
    lines[2:2] = [
        "**Latest assessment:** [Recommended measures and synchronization-quality judgment](PRESENTATION.md), including the repeat after other controlling processes were closed. [Repeat acquisition and diagnostic details](EXCLUSIVE_REPEAT.md). The main tables below retain the original experiment for comparison.",
        "",
    ]
(root / "REPORT.md").write_text("\n".join(lines))
print(f"Wrote report, overview and {len(coverage)} array/pair rows to {root}")
