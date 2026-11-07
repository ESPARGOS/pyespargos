#!/usr/bin/env python3
"""Reproducible waveform-independent DOA accuracy and timing experiment.

Run with OPENBLAS_NUM_THREADS=1 PYTHONPATH=. .venv/bin/python
 tools/benchmark_array_processing.py --trials 100 > /tmp/array-benchmark.json
Times include covariance estimation separately; exclude image rendering.
Coherent paths are a limitation diagnostic, not a recovery accuracy claim.
"""

import argparse
import itertools
import json
import platform
import time

import numpy as np
from espargos.array_processing import bartlett_fft_2d, mdl_source_count, root_music_2d, steering_vectors_2d

FREQUENCY = 2472e6


def recording(rng, shape, directions, snr_db, snapshots=256, coherent=False):
    r, c = np.indices(shape)
    directions = np.asarray(directions)
    a = np.exp(2j * np.pi * 0.06 * FREQUENCY / 299792458 * (c.ravel()[:, None] * directions[:, 0] + r.ravel()[:, None] * directions[:, 1]))

    def noise(shape):
        return (rng.normal(size=shape) + 1j * rng.normal(size=shape)) / np.sqrt(2)

    signals = noise((len(directions), snapshots))
    if coherent:
        signals[:] = signals[0]
    return a @ signals + 10 ** (-snr_db / 20) * noise((a.shape[0], snapshots))


def angular_error(estimated, expected):
    def vectors(uv):
        return np.column_stack((uv, np.sqrt(np.maximum(1 - np.sum(uv**2, axis=1), 0))))

    a, b = vectors(estimated), vectors(expected)
    return min(max(np.degrees(np.arccos(np.clip(np.dot(a[i], b[j]), -1, 1))) for i, j in enumerate(order)) for order in itertools.permutations(range(len(b))))


def timing(function, repeats=100):
    for _ in range(10):
        function()
    times = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        function()
        times.append((time.perf_counter_ns() - start) / 1e6)
    return dict(median_ms=float(np.median(times)), p95_ms=float(np.percentile(times, 95)))


def run(trials):
    rng = np.random.default_rng(2026)
    report = dict(seed=2026, numpy=np.__version__, platform=platform.platform(), snapshots=256, trials_per_case=trials, accuracy=[], timing=[], coherent_paths=[])
    for shape in [(2, 4), (6, 4)]:
        for snr in [0, 10, 20]:
            for count in [1, 2]:
                errors, misses = [], 0
                for _ in range(trials):
                    truth = np.array([[-0.4, 0.2], [0.35, -0.25]])[:count] + rng.uniform(-0.08, 0.08, (count, 2))
                    x = recording(rng, shape, truth, snr)
                    covariance = x @ x.conj().T / x.shape[1]
                    got, _, _ = root_music_2d(covariance, *shape, frequency_hz=FREQUENCY, snapshots=x.shape[1])
                    if len(got) != count:
                        misses += 1
                    else:
                        errors.append(angular_error(got, truth))
                report["accuracy"].append(dict(shape=shape, snr_db=snr, sources=count, count_mismatches=misses, p95_error_degrees=float(np.percentile(errors, 95)) if errors else None))
        truth = np.array([[-0.4, 0.2], [0.35, -0.25]])
        x = recording(rng, shape, truth, 20, coherent=True)
        covariance = x @ x.conj().T / x.shape[1]
        got, _, purity = root_music_2d(covariance, *shape, frequency_hz=FREQUENCY, snapshots=x.shape[1])
        report["coherent_paths"].append(dict(shape=shape, physical_paths=2, mdl_rank=mdl_source_count(np.linalg.eigvalsh(covariance), x.shape[1]), returned_directions=got.tolist(), purity=purity))
        x = recording(rng, shape, truth, 10)
        covariance = x @ x.conj().T / x.shape[1]
        grid = np.moveaxis(steering_vectors_2d(*shape, 128, 64, frequency_hz=FREQUENCY, pixel_centers=True).reshape(-1, 128, 64), 0, -1)

        def dense_music():
            _, vectors = np.linalg.eigh(covariance)
            return 1 / np.maximum(np.sum(abs(grid @ vectors[:, :-2].conj()) ** 2, axis=-1), 1e-12)

        methods = dict(
            covariance=lambda: x @ x.conj().T / x.shape[1],
            root_music=lambda: root_music_2d(covariance, *shape, frequency_hz=FREQUENCY, snapshots=x.shape[1]),
            fft_bartlett=lambda: bartlett_fft_2d(covariance, *shape, 128, 64, frequency_hz=FREQUENCY),
            dense_music=dense_music,
            dense_bartlett=lambda: np.einsum("uva,ab,uvb->uv", grid.conj(), covariance, grid).real,
        )
        report["timing"].append(dict(shape=shape, methods={name: timing(fn) for name, fn in methods.items()}))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=100)
    print(json.dumps(run(parser.parse_args().trials), indent=2))
