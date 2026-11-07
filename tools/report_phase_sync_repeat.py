#!/usr/bin/env python3
"""Report the repeat performed after other board controllers were closed."""

import json
import pathlib
import numpy as np

root = pathlib.Path(__file__).resolve().parents[1] / "measurements/phase_sync"
hosts = ["192.168.1.2", "172.16.0.241"]
previous = ["long_retry", "long"]
records = []
lines = [
    "# Repeat after closing other controlling processes",
    "",
    "This follow-up was run after the user reported closing any other process controlling the arrays. It repeats the same 300-second, 100 Hz, 8.5 dBm, TX-sensor-2, forced 12-bit L-LTF experiment with CFO correction disabled and frozen per-receiver analog/FFT gains. The boards remain separate clock domains. Environmental stationarity was not established.",
    "",
    "The repeat includes receiver settings and sensor debug counters at the start and end of acquisition. These endpoint checks can detect persistent configuration changes and counter/uptime changes; they cannot rule out every possible transient outside action.",
    "",
    "## Results",
    "",
    "| Array | Previous observed span | Repeat observed span | Previous median pair RMS | Repeat median pair RMS | Repeat pair RMS range |",
    "|---|---:|---:|---:|---:|---:|",
]
for host, oldrun in zip(hosts, previous):
    base = root / host / "exclusive_repeat"
    m = json.loads((base / "lltf12_tx2.json").read_text())
    d = json.loads((base / "lltf12_tx2_analysis.json").read_text())
    old = json.loads((root / host / oldrun / "lltf12_tx2_analysis.json").read_text())
    records.append((host, m, d))
    if "error" in d:
        lines.append(f'| {host} | {old["elapsed_s"]:.2f} s | excluded: {d["error"]} | {np.median(old["phase_rms_deg"]):.3f}° | — | — |')
    else:
        lines.append(f'| {host} | {old["elapsed_s"]:.2f} s | {d["elapsed_s"]:.2f} s | {np.median(old["phase_rms_deg"]):.3f}° | {np.median(d["phase_rms_deg"]):.3f}° | {min(d["phase_rms_deg"]):.3f}–{max(d["phase_rms_deg"]):.3f}° |')
lines += [
    "",
    "Phase RMS uses all captured packets in an accepted clock epoch, removing only the mean. Each run compares 21 simultaneous RX pairs. The repeat does not remeasure all 28 pairs; the earlier transmitter-rotation records provide that coverage.",
    "",
    "| Array | Complete packets | ADEV at 1 s, median | ADEV at 10 s, median | Equivalent SSB near 30 Hz, median | Largest phase deviation |",
    "|---|---:|---:|---:|---:|---:|",
]
for host, m, d in records:
    if "error" in d:
        continue
    z = np.load(root / host / "exclusive_repeat/lltf12_tx2_analysis.npz")
    phase = z["phase"] * 180 / np.pi

    def adev(tau):
        if tau not in d["taus_s"]:
            return "unavailable"
        return f'{np.median(d["adev"][d["taus_s"].index(tau)]):.3g}'

    pn = d["phase_noise_dbc_hz"].get("30")
    density = f"{np.median(pn):.2f} dBc/Hz" if pn else "unavailable"
    lines.append(f'| {host} | {d["n"]} | {adev(1)} | {adev(10)} | {density} | {np.max(abs(phase)):.2f}° |')
lines += [
    "",
    "Allan and PSD estimates use the same missing-data and timing filters as the [original method](METHOD.md). Phase-noise density is a sampled-CSI equivalent, with a 50 Hz Nyquist limit, rather than intrinsic continuous-time PLL phase noise.",
    "",
    "## Acquisition and diagnostics",
    "",
]
for host, m, d in records:
    lines += [f"### {host}", "", f'Capture ended because: **{m.get("capture_end_reason","unspecified")}**. Acquisition wall time: {m.get("wall_elapsed_s",float("nan")):.2f} seconds.', ""]
    changed = [k for k in m["settings"] if m.get("settings_end", {}).get(k) != m["settings"][k]]
    lines += [f'Start/end receiver and TX setting differences: {", ".join(changed) if changed else "none"}.', ""]
    if "error" not in d:
        gains_fixed = all(len(x) == 1 for x in d["rx_gain_unique"] + d["fft_gain_unique"])
        lines += [
            f'Per-packet RX/FFT gains fixed: {gains_fixed}. Maximum per-receiver fraction of digital CSI values near the representation limit: {max(d.get("raw_near_limit_fraction",[float("nan")])):.3g}.',
            "",
            f'Complete fraction within observed span: {100*d["complete_fraction"]:.2f}%. Fraction accepted by the timing filter: {100*d["timing_accepted_fraction"]:.2f}%. Complete PSD windows: {d["psd_windows"]}.',
            "",
        ]
    start = {s["antid"]: s for s in m["debug_start"]["sensors"]}
    end = {s["antid"]: s for s in m["debug_end"]["sensors"]}
    lines += ["| Firmware antenna ID | Uptime change (s) | Watchdog counter start → end | CSI callback count change |", "|---:|---:|---:|---:|"]
    for antid in sorted(start.keys() & end.keys()):
        s = start[antid]
        e = end[antid]
        lines.append(f'| {antid} | {(e["uptime_ms"]-s["uptime_ms"])/1000:.3f} | {s["csi_watchdog_reinits"]} → {e["csi_watchdog_reinits"]} | {e["wifi_csi_cb"]-s["wifi_csi_cb"]} |')
    status = root / host / "exclusive_repeat/final_status.json"
    if status.exists():
        differences = json.loads(status.read_text())["restoration_differences"]
        lines += ["", f'Final configuration readback compared with immediately before this repeat: {"restored exactly" if not differences else str(differences)}.']
    lines += ["", f"[Phase traces, pair RMS, spectrum, and Allan plot]({host}/exclusive_repeat/lltf12_tx2_analysis.png). [Full capture metadata and diagnostics]({host}/exclusive_repeat/lltf12_tx2.json).", ""]
lines += [
    "## Interpretation",
    "",
    "Both arrays completed the requested five minutes. All sensor uptimes advanced by approximately 300 seconds; the capture detected no backwards timestamp jump. RX/FFT gains stayed fixed, no digital CSI near-limit values were seen, and endpoint settings matched. Thus the earlier acquisition interruption was not reproduced, although this does not prove that another process caused it.",
    "",
    "The CSI watchdog did continue firing on the transmitting sensor, firmware antenna ID 6 (row-major sensor 2): five in-place reinitializations on 192.168.1.2 and twelve on 172.16.0.241. Other sensor watchdog counters did not increase during the capture. The TX sensor produced only one/zero received CSI callbacks while the receiving sensors produced approximately 30,000 each. This pattern suggests a radar-TX/watchdog interaction; it is not evidence that the seven receivers lost phase lock. The counters show that closing other controllers did not eliminate watchdog activity. No firmware changes were made.",
    "",
    "The new phase-error records still contain excursions, so the successful acquisition does not turn the earlier degree-level RMS results into a hard phase bound. See [recommended presentation and quality judgment](PRESENTATION.md), which includes the tail behavior rather than relying on Allan deviation alone. Historical captures and exclusions remain unchanged.",
    "",
]
(root / "EXCLUSIVE_REPEAT.md").write_text("\n".join(lines))
print(root / "EXCLUSIVE_REPEAT.md")
