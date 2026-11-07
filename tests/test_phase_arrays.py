import unittest
import numpy as np
from demos.common.phase_arrays import phase_arrays, sample_covariances


class PhaseArraysTests(unittest.TestCase):
    def test_random_feeds_have_independent_phase_references(self):
        rng = np.random.default_rng(18)
        phases = np.array([[0, .2, .4, .6, .8, 1., 1.2, 1.4],
                           [0, -.3, -.6, -.9, -1.2, -1.5, -1.8, -2.1]])
        feeds = rng.integers(2, 4, (80, 8))
        packet_phases = rng.uniform(-np.pi, np.pi, (80, 1))
        values = np.exp(1j*(phases[feeds-2, np.arange(8)] + packet_phases))
        samples = np.repeat(values[..., None], 20, axis=-1)
        covariance, counts = sample_covariances(samples)
        panels = phase_arrays(covariance, counts, feeds, split=True)
        self.assertEqual(len(panels), 2)
        for panel, expected in zip(panels, phases):
            actual = [float(label.split()[0]) for label in panel['labels']]
            np.testing.assert_allclose(actual, expected, atol=.01)
            self.assertTrue(all(label.endswith(' rad') for label in panel['labels']))

    def test_fixed_feed_and_disconnected_sensors(self):
        samples = np.exp(1j*np.arange(8)[None, :, None]*.2)*np.ones((2, 8, 10))
        samples[:, 7] = np.nan
        covariance, counts = sample_covariances(samples)
        panel, = phase_arrays(covariance, counts, np.full((2, 8), 2))
        self.assertEqual(panel['labels'][7], '—')
        self.assertEqual(panel['labels'][3], '+0.60 rad')
        split = phase_arrays(covariance, counts, np.full((2, 8), 2), True)
        self.assertEqual(split[1]['labels'], ['—']*8)

    def test_separate_groups_do_not_invent_relative_phase(self):
        samples = np.ones((2, 8, 10), complex)
        samples[0, 4:] = np.nan
        samples[1, :4] = np.nan
        covariance, counts = sample_covariances(samples)
        panel, = phase_arrays(covariance, counts, np.full((2, 8), 2))
        self.assertEqual(panel['labels'][4:], ['—']*4)
