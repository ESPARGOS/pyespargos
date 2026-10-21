"""Generate the phase and delay illustration."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

INK, GREEN, GREY = "#293135", "#227b3d", "#8b969a"
plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["Carlito", "DejaVu Sans"],
        "font.size": 13,
        "text.color": INK,
        "axes.labelcolor": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "axes.edgecolor": GREY,
        "axes.linewidth": 0.8,
        "svg.fonttype": "path",
        "svg.hashsalt": "calibration-offsets",
        "mathtext.fontset": "dejavusans",
    }
)
fig, axes = plt.subplots(1, 3, figsize=(12, 4))
fig.subplots_adjust(left=0.045, right=0.985, bottom=0.26, top=0.84, wspace=0.48)
phi, tau_ns = np.pi / 4, 10
arrow = dict(arrowstyle="->", lw=1.6, mutation_scale=14, shrinkA=0, shrinkB=0)

ax = axes[0]
angle = np.linspace(0, np.pi / 2, 100)
ax.plot(np.cos(angle), np.sin(angle), color="#d0d8d4", lw=0.8)
for a, color in [(0, INK), (phi, GREEN)]:
    ax.annotate("", xy=(np.cos(a), np.sin(a)), xytext=(0, 0), arrowprops={**arrow, "color": color})
arc = np.linspace(0, phi, 40)
ax.plot(0.36 * np.cos(arc), 0.36 * np.sin(arc), color=INK, lw=0.8)
ax.text(0.42, 0.12, r"$\phi$", color=INK)
ax.text(0.88, 0.07, r"$x$", ha="center")
ax.text(0.73, 0.77, r"$e^{j\phi}x$", color=GREEN)
ax.set(xlim=(-0.12, 1.18), ylim=(-0.12, 1.18), aspect="equal")
ax.spines[["left", "bottom"]].set_position("zero")
ax.set_xticks([1])
ax.set_yticks([1])
ax.text(1.15, -0.14, "I", ha="center")
ax.text(-0.1, 1.13, "Q", ha="center")
ax.set_title("Phase rotation", pad=20, fontsize=15)

ax = axes[1]
for t, color, label in [(0, INK, r"$\delta(t)$"), (tau_ns, GREEN, r"$\delta(t-\tau)$")]:
    ax.annotate("", xy=(t, 1), xytext=(t, 0), arrowprops={**arrow, "color": color})
    ax.text(t, 1.08, label, ha="center", color=color)
ax.annotate("", xy=(tau_ns, 0.45), xytext=(0, 0.45), arrowprops=dict(arrowstyle="<->", color=GREY, lw=0.9, shrinkA=4, shrinkB=4))
ax.text(tau_ns / 2, 0.51, r"$\tau = 10\,\mathrm{ns}$", ha="center", fontsize=12)
ax.set(xlabel="Time (ns)", ylim=(-0.12, 1.18), xlim=(-5, 15))
ax.spines["bottom"].set_position("zero")
ax.spines["left"].set_visible(False)
ax.set_xticks([0, 10])
ax.set_yticks([])
ax.set_title("Time delay", pad=20, fontsize=15)

ax = axes[2]
f_mhz = np.linspace(-20, 20, 200)
delay_phase = -360 * f_mhz * 1e6 * tau_ns * 1e-9
ax.plot(f_mhz, np.degrees(phi) + delay_phase, color=GREEN, lw=1.6, label="Before correction")
ax.plot(f_mhz, delay_phase, color=GREY, lw=1.3, ls="--", label="Phase corrected")
ax.axhline(0, color=INK, ls=":", lw=1.5, label="Phase and delay corrected")
ax.set(xlabel="Offset from carrier (MHz)", ylabel="Phase (°)", xlim=(-20, 20), ylim=(-85, 135))
ax.set_xticks([-20, 0, 20])
ax.set_yticks([-45, 0, 45, 90])
ax.set_title("Frequency response", pad=20, fontsize=15)
ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.29), frameon=False, fontsize=11, borderaxespad=0, handlelength=2)
for ax in axes:
    ax.spines[["top", "right"]].set_visible(False)
output = Path(__file__).resolve().parents[1] / "_static/calibration-offsets.svg"
fig.savefig(output, bbox_inches="tight", metadata={"Date": None})
output.write_text("\n".join(line.rstrip() for line in output.read_text().splitlines()) + "\n")
