#!/usr/bin/env python3
"""Receive-only stepped HackRF spectrum capture; does not configure the radar.

Example: python tools/hackrf_radar_spectrum.py capture.npz
Uses ordinary RX transfers, including on firmware without sweep support.
Power is uncalibrated dBFS/Hz, not dBm. Retuning makes this unsuitable for
transients. Keep geometry and gains fixed when comparing steady waveforms.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile

import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--centers", nargs="+", type=float, default=list(range(2270, 2511, 10)), help="Tuning centers in MHz")
    parser.add_argument("--lna", type=int, choices=range(0, 41, 8), default=16)
    parser.add_argument("--vga", type=int, choices=range(0, 63, 2), default=16)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    if not all(np.isfinite(c) and 1 <= c <= 6000 for c in args.centers):
        parser.error("centers must be finite frequencies from 1 to 6000 MHz")
    rate, size, count = 20_000_000, 8192, 1_048_576
    window = np.hanning(size)
    offsets = np.fft.fftfreq(size, 1 / rate)
    # Only use the central 10 MHz of the 15 MHz analog filter. Omit LO DC;
    # the saved frequency vector explicitly contains these measurement gaps.
    keep = (abs(offsets) < 5e6) & (abs(offsets) > 100e3)
    spectra, quality, commands, logs = [], [], [], []
    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="hackrf-radar-") as temporary:
        raw_path = Path(temporary) / "iq.bin"
        for center in args.centers:
            command = ["hackrf_transfer", "-r", str(raw_path), "-f", str(round(center * 1e6)), "-s", str(rate), "-b", "15000000", "-l", str(args.lna), "-g", str(args.vga), "-a", "0", "-p", "0", "-n", str(count)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise RuntimeError(result.stderr)
            raw = np.fromfile(raw_path, np.int8)
            if raw.size != 2 * count:
                raise RuntimeError(f"Short IQ capture at {center} MHz: {raw.size} bytes")
            raw = raw.reshape(-1, 2).astype(float)
            quality.append([center, float(abs(raw).max()), float(np.mean(abs(raw) >= 127))])
            iq = (raw[:, 0] + 1j * raw[:, 1]) / 128
            iq = iq[131072:]  # Discard initial settling samples.
            blocks = iq[: len(iq) // size * size].reshape(-1, size)
            psd = np.mean(abs(np.fft.fft(blocks * window, axis=1)) ** 2, axis=0) / (rate * np.sum(window**2))
            spectra.append(np.array([center * 1e6 + offsets[keep], psd[keep]]))
            commands.append(command)
            logs.append(result.stderr)
            print(f"{center:g} MHz: peak {quality[-1][1]:g}/128, rail fraction {quality[-1][2]:.3g}", flush=True)
    metadata = dict(started_utc=started, commands=commands, transfer_logs=logs, quality_columns=["center_mhz", "peak_component_counts", "rail_fraction"], spectrum_rows=["frequency_hz", "power_fs_squared_per_hz"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Use a handle so NumPy does not silently append a filename extension.
    with args.output.open("xb") as destination:
        np.savez_compressed(destination, spectrum=np.concatenate(spectra, axis=1), quality=quality, lna=args.lna, vga=args.vga, rate=rate, metadata=json.dumps(metadata))


if __name__ == "__main__":
    main()
