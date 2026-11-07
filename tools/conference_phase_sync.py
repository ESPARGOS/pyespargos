#!/usr/bin/env python3
"""Literature-standard stability figures from existing differential CSI phases."""

import argparse
import csv
import json
import os
import pathlib
import shutil
import subprocess

os.environ.setdefault("MPLCONFIGDIR", "/tmp/espargos-matplotlib")
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1] / "measurements/phase_sync"
DT = 0.01
MAD_SCALE = 1.482602218505602


def overlapping_adev(phase, carrier_hz, factors, dt=DT):
    """NIST phase-data estimator; omit only terms with missing endpoints."""
    values = []
    counts = []
    for m in factors:
        second = phase[2 * m :] - 2 * phase[m:-m] + phase[: -2 * m]
        finite = np.isfinite(second)
        n = finite.sum(axis=0)
        variance = np.sum(np.where(finite, second**2, 0), axis=0) / np.maximum(n, 1)
        value = np.sqrt(variance / 2) / (2 * np.pi * carrier_hz * m * dt)
        value[n == 0] = np.nan
        values.append(value)
        counts.append(n)
    return np.array(values), np.array(counts)


def block_average(phase, samples=100, minimum_fraction=0.8):
    n = len(phase) // samples
    blocks = phase[: n * samples].reshape(n, samples, phase.shape[1])
    count = np.isfinite(blocks).sum(axis=1)
    avg = np.nansum(blocks, axis=1) / np.maximum(count, 1)
    avg[count < samples * minimum_fraction] = np.nan
    return avg, count


def bootstrap_median_adev(phase, carrier_hz, factors, repetitions=2000, seed=180926):
    """Pointwise circular moving-block percentile intervals, conditional on screening.

    Resample squared second-difference terms, not stitched phase records, so no
    artificial phase discontinuities enter the estimator. Shared block indices
    retain cross-pair dependence. Long-tau intervals are exploratory.
    """
    rng = np.random.default_rng(seed)
    intervals = []
    lengths = []
    for m in factors:
        second = phase[2 * m :] - 2 * phase[m:-m] + phase[: -2 * m]
        valid = np.isfinite(second)
        square = np.where(valid, second**2, 0)
        n = len(second)
        block = min(max(500, 2 * int(m)), n)
        whole, remainder = divmod(n, block)
        sums = np.vstack([np.zeros((1, phase.shape[1])), np.cumsum(np.tile(square, (2, 1)), axis=0)])
        counts = np.vstack([np.zeros((1, phase.shape[1])), np.cumsum(np.tile(valid, (2, 1)), axis=0)])
        estimates = []
        for first in range(0, repetitions, 100):
            size = min(100, repetitions - first)
            starts = rng.integers(0, n, size=(size, whole))
            total = (sums[starts + block] - sums[starts]).sum(axis=1)
            count = (counts[starts + block] - counts[starts]).sum(axis=1)
            if remainder:
                start = rng.integers(0, n, size=size)
                total += sums[start + remainder] - sums[start]
                count += counts[start + remainder] - counts[start]
            values = np.sqrt(total / (2 * np.maximum(count, 1))) / (2 * np.pi * carrier_hz * m * DT)
            values[count == 0] = np.nan
            estimates.extend(np.nanmedian(values, axis=1))
        intervals.append(np.quantile(estimates, [0.025, 0.975]).tolist())
        lengths.append(block * DT)
    return dict(method="circular moving-block bootstrap of squared phase second differences; joint pair resampling", confidence=0.95, repetitions=repetitions, seed=seed, block_s=lengths, lower_upper=intervals)


def two_point_phase_deviation(phase, lags):
    """ALMA-style two-point phase ASD: sqrt(mean((phi(t+T)-phi(t))^2)/2)."""
    values = []
    counts = []
    for lag in lags:
        delta = phase[lag:] - phase[:-lag]
        n = np.isfinite(delta).sum(axis=0)
        value = np.sqrt(np.nansum(delta**2, axis=0) / (2 * np.maximum(n, 1)))
        value[n == 0] = np.nan
        values.append(value)
        counts.append(n)
    return np.array(values), np.array(counts)


def load_record(host):
    base = ROOT / host / "exclusive_repeat"
    data = np.load(base / "lltf12_tx2_analysis.npz")
    meta = json.loads((base / "lltf12_tx2.json").read_text())
    t = data["t"]
    phase = data["phase"]
    pairs = data["pairs"]
    carrier = (2407 + 5 * meta["settings"]["wifi"]["channel-primary"]) * 1e6
    offset = np.angle(np.mean(np.exp(2j * np.pi * t / DT))) * DT / (2 * np.pi)
    slot = np.rint((t - offset) / DT).astype(int)
    residual = t - slot * DT - offset
    unique, multiplicity = np.unique(slot, return_counts=True)
    time_ok = (abs(residual) <= 0.0005) & ~np.isin(slot, unique[multiplicity > 1])
    raw = np.full((slot.max() + 1, phase.shape[1]), np.nan)
    raw[slot[time_ok]] = phase[time_ok]
    center = np.median(phase, axis=0)
    robust_sigma = MAD_SCALE * np.median(abs(phase - center), axis=0)
    rejected = abs(phase - center) > 5 * robust_sigma
    screened = raw.copy()
    screened[slot[time_ok]] = np.where(rejected[time_ok], np.nan, phase[time_ok])
    factors = np.array([1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 3000])
    factors = factors[factors * DT <= t[-1] / 10]
    lags = np.array([1, 2, 5, 10, 20, 30])
    lags = lags[lags <= t[-1] / 10]
    result = dict(
        host=host,
        carrier_hz=carrier,
        observed_s=float(t[-1]),
        packets=len(t),
        pair_labels=[f"{i}-{j}" for i, j in pairs],
        tau_s=(factors * DT).tolist(),
        time_rejected_fraction=float(np.mean(~time_ok)),
        outlier_screen="one-pass per-pair |phi-median(phi)| > 5*1.4826022185*MAD(phi)",
        phase_outlier_fraction=np.mean(rejected, axis=0).tolist(),
        additional_outlier_fraction_after_time_gate=np.mean(rejected[time_ok], axis=0).tolist(),
        phase_average_s=1.0,
        phase_average_minimum_fraction=0.8,
        phase_lag_s=lags.tolist(),
    )
    for name, g in [("screened", screened), ("unscreened", raw)]:
        adev, count = overlapping_adev(g, carrier, factors)
        avg, bin_count = block_average(g)
        phase_asd, phase_count = two_point_phase_deviation(avg, lags)
        result[name] = dict(
            adev=adev.tolist(),
            avar=(adev**2).tolist(),
            adev_terms=count.tolist(),
            phase_asd_deg=np.rad2deg(phase_asd).tolist(),
            equivalent_time_asd_ps=(phase_asd / (2 * np.pi * carrier) * 1e12).tolist(),
            phase_terms=phase_count.tolist(),
            valid_phase_bins=np.isfinite(avg).sum(axis=0).tolist(),
            mean_bin_sample_count=np.mean(bin_count, axis=0).tolist(),
        )
    if host == "192.168.1.2":
        result["median_adev_ci"] = bootstrap_median_adev(screened, carrier, factors)
    return result


def export(fig, path):
    for ext in ["pdf", "svg", "png"]:
        fig.savefig(path.with_suffix("." + ext), dpi=600, bbox_inches="tight")


def draw(ax, records, kind, unscreened=False):
    # Conference panels show only the better array's median; keep both records
    # in the data exports and the explicitly separate screening diagnostic.
    if not unscreened:
        records = [d for d in records if d["host"] == "192.168.1.2"]
    for d, color, marker in zip(records, ["#0072B2", "#D55E00"], ["o", "s"]):
        if kind == "adev":
            x = np.array(d["tau_s"])
            y = np.array(d["screened"]["adev"])
            yr = np.array(d["unscreened"]["adev"])
        else:
            x = np.array(d["phase_lag_s"])
            y = np.array(d["screened"]["phase_asd_deg"])
            yr = np.array(d["unscreened"]["phase_asd_deg"])
        ax.loglog(x, np.median(y, axis=1), color=color, marker=marker, ms=4, lw=1.8, label=d["host"])
        if kind == "adev" and not unscreened:
            bounds = np.array(d["median_adev_ci"]["lower_upper"])
            # Draw interval endpoints directly: percentile intervals need not
            # mathematically contain the original sample estimate.
            ax.vlines(x, bounds[:, 0], bounds[:, 1], color=color, lw=1.1)
            ax.plot(x, bounds[:, 0], linestyle="none", marker="_", ms=7, color=color)
            ax.plot(x, bounds[:, 1], linestyle="none", marker="_", ms=7, color=color)
        if unscreened:
            ax.loglog(x, np.median(yr, axis=1), color=color, ls="--", lw=1.3, label=d["host"] + " unscreened")
    if kind == "adev":
        ax.set(xlabel=r"Averaging time $\tau$ (s)", ylabel=r"Overlapping Allan deviation $\sigma_y(\tau)$")
        ax.set_title("Residual fractional-frequency stability", fontsize=10)
        if not unscreened:
            ax.text(0.03, 0.04, "Median of 21 RX pairs\nApprox. 95% pointwise bootstrap CI", transform=ax.transAxes, fontsize=8, va="bottom")
    else:
        ax.set(xlabel=r"Separation $T$ (s)", ylabel=r"Two-point phase deviation $\sigma_\phi(T)$ (deg)")
        ax.set_title("Phase stability; fixed 1 s averaging", fontsize=10)
    ax.grid(which="major", alpha=0.25, lw=0.5)
    ax.grid(which="minor", alpha=0.10, lw=0.4)
    if unscreened:
        ax.legend(frameon=False, fontsize=8, loc="best")


def self_test():
    rng = np.random.default_rng(118)
    n = 100000
    fc = 2.447e9
    noise = 0.01
    phase = rng.normal(0, noise, (n, 1))
    factors = np.array([1, 10, 100])
    actual, _ = overlapping_adev(phase, fc, factors)
    expected = np.sqrt(3) * noise / (2 * np.pi * fc * factors * DT)
    np.testing.assert_allclose(actual[:, 0], expected, rtol=0.02)
    # Linear phase ramp (constant frequency offset) must vanish in ADEV.
    ramp = np.arange(n)[:, None] * 0.003
    ramp_result, _ = overlapping_adev(ramp, fc, factors)
    assert np.max(ramp_result) < 1e-18
    # Missing endpoints are omitted without joining or filling the timeline.
    gapped = phase.copy()
    gapped[::13] = np.nan
    gap_result, gap_count = overlapping_adev(gapped, fc, [10])
    terms = []
    for i in range(n - 20):
        v = gapped[[i, i + 10, i + 20], 0]
        if np.all(np.isfinite(v)):
            terms.append((v[2] - 2 * v[1] + v[0]) ** 2)
    np.testing.assert_allclose(gap_result[0, 0], np.sqrt(np.mean(terms) / 2) / (2 * np.pi * fc * 0.1), rtol=1e-12)
    assert gap_count[0, 0] == len(terms)
    avg, _ = block_average(phase)
    pd, _ = two_point_phase_deviation(avg, [1, 2, 5])
    np.testing.assert_allclose(pd[:, 0], noise / 10, rtol=0.08)
    single = bootstrap_median_adev(phase[:3000], fc, [1, 10], repetitions=200)
    duplicate = bootstrap_median_adev(np.repeat(phase[:3000], 3, axis=1), fc, [1, 10], repetitions=200)
    np.testing.assert_allclose(single["lower_upper"], duplicate["lower_upper"])
    assert np.all(np.diff(np.array(single["lower_upper"]), axis=1) > 0)
    print("PASS: white-phase scaling, constant-frequency rejection, gap handling, fixed-average phase ASD")
    print("PASS: bootstrap interval ordering and joint-pair resampling (duplicated pairs add no information)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    out = ROOT / "conference"
    out.mkdir(exist_ok=True)
    records = [load_record(h) for h in ["192.168.1.2", "172.16.0.241"]]
    (out / "results.json").write_text(json.dumps(records, indent=2))
    d = records[0]
    with (out / "allan_deviation_median_ci.csv").open("w") as stream:
        w = csv.writer(stream)
        w.writerow(["host", "tau_s", "median_adev", "approx_95pct_lower", "approx_95pct_upper", "bootstrap_block_s"])
        for tau, value, bounds, block in zip(d["tau_s"], np.median(d["screened"]["adev"], axis=1), d["median_adev_ci"]["lower_upper"], d["median_adev_ci"]["block_s"]):
            w.writerow([d["host"], tau, value, *bounds, block])
    with (out / "allan_deviation.csv").open("w") as stream:
        w = csv.writer(stream)
        w.writerow(["host", "pair", "tau_s", "screened_adev", "screened_avar", "valid_terms", "unscreened_adev"])
        for d in records:
            for k, tau in enumerate(d["tau_s"]):
                for j, pair in enumerate(d["pair_labels"]):
                    w.writerow([d["host"], pair, tau, d["screened"]["adev"][k][j], d["screened"]["avar"][k][j], d["screened"]["adev_terms"][k][j], d["unscreened"]["adev"][k][j]])
    with (out / "phase_deviation.csv").open("w") as stream:
        w = csv.writer(stream)
        w.writerow(["host", "pair", "fixed_average_s", "separation_s", "screened_phase_asd_deg", "screened_time_asd_ps", "valid_terms", "unscreened_phase_asd_deg"])
        for d in records:
            for k, lag in enumerate(d["phase_lag_s"]):
                for j, pair in enumerate(d["pair_labels"]):
                    w.writerow([d["host"], pair, 1, lag, d["screened"]["phase_asd_deg"][k][j], d["screened"]["equivalent_time_asd_ps"][k][j], d["screened"]["phase_terms"][k][j], d["unscreened"]["phase_asd_deg"][k][j]])
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.labelsize": 10, "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none"})
    # The publication figure is rendered by PGFPlots, not Matplotlib.
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "allan_deviation.tex"], cwd=out, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    subprocess.run(["pdftoppm", "-png", "-singlefile", "-r", "300", "allan_deviation.pdf", "allan_deviation"], cwd=out, check=True)
    subprocess.run(["pdftocairo", "-svg", "allan_deviation.pdf", "allan_deviation.svg"], cwd=out, check=True)
    for extension in ["pdf", "png", "svg"]:
        shutil.copyfile(out / ("allan_deviation." + extension), out / ("stability." + extension))
    fig, ax = plt.subplots(figsize=(5.4, 3.8), layout="constrained")
    draw(ax, records, "adev", unscreened=True)
    export(fig, out / "screening_sensitivity")
    plt.close(fig)
    for d in records:
        print(d["host"], "rejected phase fraction median/max", np.median(d["phase_outlier_fraction"]), max(d["phase_outlier_fraction"]))
        print("ADEV medians", dict(zip(d["tau_s"], np.median(d["screened"]["adev"], axis=1))))
        print("Phase ASD medians", dict(zip(d["phase_lag_s"], np.median(d["screened"]["phase_asd_deg"], axis=1))))


if __name__ == "__main__":
    main()
