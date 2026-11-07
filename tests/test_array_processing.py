"""Numerical edge cases shared by every camera's spatial processing."""
import unittest
import numpy as np
from espargos.array_processing import music_spectrum, steering_vectors_2d


class MusicTests(unittest.TestCase):
    def test_exact_rank_one_and_zero_covariance_remain_finite(self):
        steering = steering_vectors_2d(2, 4, 33, 25, frequency_hz=2402e6)
        target = (11, 14)
        vector = steering[:, :, target[0], target[1]].ravel()
        with np.errstate(divide='raise', invalid='raise'):
            power = music_spectrum(np.outer(vector, vector.conj()), steering)
            empty = music_spectrum(np.zeros((8, 8)), steering)
        self.assertTrue(np.isfinite(power).all())
        self.assertTrue(np.isfinite(empty).all())
        self.assertEqual(np.unravel_index(power.argmax(), power.shape), target)
