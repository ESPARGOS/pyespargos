#!/usr/bin/env python3
"""Reproducible BLE polarization and multipath stress tests without hardware.

Run from the repository root with PYTHONPATH=. python tools/benchmark_ble_processing.py.
Reports reconstruction errors and rejected windows separately. These synthetic
results test the algorithms, not antenna calibration or real-world accuracy.
"""
import argparse
import json
import numpy as np
from espargos.ble_phase_history import BLEPhaseHistory
from espargos.array_processing import root_music_2d, spatial_smoothing_2d
from espargos import constants


def polarization(rng, trials):
    results = []
    for size in (8, 16, 24):
        for noise in (.01, .1, .3):
            for ratio in (1, .1):
                errors = []
                for _ in range(trials):
                    field = np.exp(1j * rng.normal(size=(8, 2))) * [1, ratio]
                    history = BLEPhaseHistory(size=size, max_age=60)
                    for _ in range(size):
                        inputs = rng.integers(2, 4, 8)
                        values = field[np.arange(8), inputs - 2]
                        values = values + noise * (rng.normal(size=8) + 1j * rng.normal(size=8))
                        values *= rng.uniform(.3, 3) * np.exp(1j * rng.uniform(-np.pi, np.pi))
                        history.append(values, inputs)
                    estimate = history.coherent_feeds()
                    if estimate is not None:
                        scale = np.vdot(estimate, field) / np.vdot(estimate, estimate)
                        errors.append(float(np.linalg.norm(estimate * scale - field) / np.linalg.norm(field)))
                results.append(dict(observations=size, noise_std=noise, feed_ratio=ratio,
                    accepted=len(errors), rejected=trials-len(errors),
                    p95_relative_field_error=float(np.quantile(errors, .95)) if errors else None))
    return results


def multipath(rng, trials):
    results = []
    r, c = np.indices((2, 4))
    frequency = 2402e6
    scale = 2 * np.pi * constants.ANTENNA_SEPARATION * frequency / constants.SPEED_OF_LIGHT
    for separation in (.15, .3, .6, .9):
        for echo in (.2, .5, .8):
            errors, counts = [], []
            for _ in range(trials):
                center = rng.uniform(-.3, .3, 2)
                targets = center + np.array([[-separation/2, -.1], [separation/2, .1]])
                manifold = np.exp(1j * scale * (c.ravel()[:, None] * targets[:, 0] + r.ravel()[:, None] * targets[:, 1]))
                field = manifold @ [1, echo * np.exp(1j * rng.uniform(-np.pi, np.pi))]
                samples = field[:, None] + .1 * (rng.normal(size=(8, 16)) + 1j * rng.normal(size=(8, 16)))
                covariance, rows, columns = spatial_smoothing_2d(samples @ samples.conj().T / 16, 2, 4)
                found, _, _ = root_music_2d(covariance, rows, columns, frequency_hz=frequency,
                    snapshots=16, dynamic_range_db=10)
                counts.append(len(found))
                if len(found):
                    errors.append(float(np.min(np.linalg.norm(found - targets[0], axis=1))))
            results.append(dict(horizontal_cosine_separation=separation, echo_amplitude=echo,
                path_counts={n: counts.count(n) for n in sorted(set(counts))},
                p95_direct_cosine_error=float(np.quantile(errors, .95)) if errors else None))
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trials', type=int, default=100)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error('--trials must be positive')
    rng = np.random.default_rng(9841)
    print(json.dumps(dict(seed=9841, trials=args.trials, numpy=np.__version__,
        polarization=polarization(rng, args.trials), multipath=multipath(rng, args.trials)), indent=2))
