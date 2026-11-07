"""Range-grid validation, including a moving target beyond the old display."""
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest

path = Path(__file__).resolve().parents[1] / "demos/iq/mimo-radar/mimo_processing.py"
spec = importlib.util.spec_from_file_location("mimo_range_processing", path)
radar = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = radar
spec.loader.exec_module(radar)


def test_range_defaults_and_ambiguity_limits():
    for rate in radar.SAMPLE_RATES:
        sampling = radar.Sampling(rate)
        assert sampling.display_range() == min(15 * radar.RATE / rate, int(sampling.unambiguous_range))
        assert sampling.display_range(1e6) == int(sampling.unambiguous_range)
        assert sampling.display_range(1e6) < sampling.unambiguous_range
    for invalid in (-1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            radar.Sampling().display_range(invalid)


def test_distant_moving_target_reaches_both_maps():
    positions = np.array([(x * .06, y * .06) for y in range(2) for x in range(4)])
    p = radar.MIMOProcessor(positions, sample_rate=20e6, range_maximum=900, history=16)
    mask = np.repeat(p.masks, p.receiver_count, axis=0)
    target_range = 600
    target = mask * np.exp(-2j * np.pi * p.offsets * 2 * target_range / radar.C)
    for i in range(p.history):
        p.push(100 * mask + target * np.exp(2j * np.pi * 3 * i / p.history), i * p.interval_chunks)
    for power in p.maps():
        peak_range = p.ranges[np.argmax(power.max(axis=1))]
        assert abs(peak_range - target_range) <= 900 / 120
