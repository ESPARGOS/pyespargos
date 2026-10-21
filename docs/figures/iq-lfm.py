"""Plot a locally synthesized chirp for the IQ transmission guide."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from espargos.waveforms import LFM

plt.rcParams.update({"font.family": "sans-serif", "font.size": 12, "svg.fonttype": "path", "svg.hashsalt": "iq-lfm"})
iq = LFM(start_hz=-20e6, stop_hz=20e6, duration_s=1e-6, delay_s=0.2e-6).synthesize(samples=256)
t = np.arange(iq.size) / 80
fig, axes = plt.subplots(2, 1, figsize=(8, 3.5), sharex=True, layout="constrained")
for ax, values, label, color in zip(axes, (iq.real, iq.imag), ("I", "Q"), ("#227b3d", "#293135")):
    ax.plot(t, values, color=color, linewidth=1, marker=".", markersize=3)
    ax.set_ylabel(label)
    ax.set_ylim(-1.15, 1.15)
    ax.set_yticks([-1, 0, 1])
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e5e9e6", linewidth=0.7)
axes[-1].set_xlabel("Time (µs)")
axes[-1].set_xlim(0, 3.2)
output = Path(__file__).resolve().parents[1] / "_static" / "iq-lfm.svg"
fig.savefig(output, metadata={"Date": None})
output.write_text("\n".join(line.rstrip() for line in output.read_text().splitlines()) + "\n")
