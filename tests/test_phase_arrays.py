import unittest
import numpy as np
from demos.common.phase_arrays import phase_arrays, sample_covariances


def phase_values(panel):
    values = []
    for label in panel["labels"]:
        try:
            values.append(float(label.removesuffix("rad").strip()))
        except ValueError:
            values.append(np.nan)
    return np.array(values)


class PhaseArraysTests(unittest.TestCase):
    def test_random_feeds_have_independent_phase_references(self):
        rng = np.random.default_rng(18)
        phases = np.array([[0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4], [0, -0.3, -0.6, -0.9, -1.2, -1.5, -1.8, -2.1]])
        feeds = rng.integers(2, 4, (80, 8))
        packet_phases = rng.uniform(-np.pi, np.pi, (80, 1))
        values = np.exp(1j * (phases[feeds - 2, np.arange(8)] + packet_phases))
        samples = np.repeat(values[..., None], 20, axis=-1)
        covariance, counts = sample_covariances(samples)
        panels = phase_arrays(covariance, counts, feeds, split=True)
        self.assertEqual(len(panels), 2)
        for panel, expected in zip(panels, phases):
            np.testing.assert_allclose(phase_values(panel), expected, atol=0.01)

    def test_fixed_feed_and_disconnected_sensors(self):
        samples = np.exp(1j * np.arange(8)[None, :, None] * 0.2) * np.ones((2, 8, 10))
        samples[:, 7] = np.nan
        covariance, counts = sample_covariances(samples)
        (panel,) = phase_arrays(covariance, counts, np.full((2, 8), 2))
        self.assertTrue(np.isnan(phase_values(panel)[7]))
        self.assertAlmostEqual(phase_values(panel)[3], 0.60, places=2)
        split = phase_arrays(covariance, counts, np.full((2, 8), 2), True)
        self.assertTrue(np.isnan(phase_values(split[1])).all())

    def test_separate_groups_do_not_invent_relative_phase(self):
        samples = np.ones((2, 8, 10), complex)
        samples[0, 4:] = np.nan
        samples[1, :4] = np.nan
        covariance, counts = sample_covariances(samples)
        (panel,) = phase_arrays(covariance, counts, np.full((2, 8), 2))
        self.assertTrue(np.isnan(phase_values(panel)[4:]).all())

    def test_isolated_reference_does_not_hide_connected_antennas(self):
        phases = np.arange(8) * 0.2
        samples = np.exp(1j * phases)[None, :, None] * np.ones((2, 8, 10))
        samples[0, 1:] = np.nan
        samples[1, 0] = np.nan
        covariance, counts = sample_covariances(samples)
        (panel,) = phase_arrays(covariance, counts, np.full((2, 8), 2))
        np.testing.assert_allclose(phase_values(panel)[:3], [np.nan, 0, 0.2], atol=0.01)
        (panel,) = phase_arrays(covariance[:1], counts[:1], np.full((1, 8), 2))
        self.assertTrue(np.isnan(phase_values(panel)).all())
