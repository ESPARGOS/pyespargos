#!/usr/bin/env python3
"""Synthetic check of phase extraction, common-mode rejection and units."""

import json
import pathlib
import tempfile
import numpy as np
from analyze_phase_sync import analyze

rng = np.random.default_rng(1729)
n = 6000
sigma = 0.01
fc = 2447e6
phase = rng.normal(0, sigma, (n, 8))
common = rng.normal(0, 0.7, (n, 1, 1))
slope = rng.normal(0, 0.002, (n, 8, 1))
k = np.arange(53) - 26
channel = rng.uniform(-np.pi, np.pi, (1, 8, 53))
z = 100 * np.exp(1j * (phase[..., None] + common + slope * k + channel))
with tempfile.TemporaryDirectory(prefix="espargos-phase-validation-") as directory:
    path = pathlib.Path(directory) / "synthetic.npz"
    np.savez(path, csi=z, t=np.arange(n) * 0.01, rx_gain=np.ones((n, 8)), fft_gain=np.ones((n, 8)), rssi=np.full((n, 8), -40))
    path.with_suffix(".json").write_text(json.dumps(dict(host="synthetic", format="lltf12", tx=2, settings={"wifi": {"channel-primary": 8}})))
    analyze(path)
    d = json.loads(path.with_name("synthetic_analysis.json").read_text())
    expected_rms = np.sqrt(2) * sigma * 180 / np.pi
    measured_rms = np.median(d["phase_rms_deg"])
    expected_adev = np.sqrt(3) * np.sqrt(2) * sigma / (2 * np.pi * fc * 0.01)
    measured_adev = np.median(d["adev"][0])
    expected_l = 10 * np.log10(2 * sigma**2 / 100)
    measured_l = np.median(d["phase_noise_dbc_hz"]["10"])
    assert abs(measured_rms / expected_rms - 1) < 0.05
    assert abs(measured_adev / expected_adev - 1) < 0.05
    assert abs(measured_l - expected_l) < 1
    print(json.dumps(dict(rms_deg=[measured_rms, expected_rms], adev_10ms=[measured_adev, expected_adev], ssb_dbc_hz=[measured_l, expected_l], status="passed"), indent=2))
