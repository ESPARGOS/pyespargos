import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from espargos.array_processing import steering_vectors_2d, root_music_2d, bartlett_fft_2d
from espargos._iq_calibration import reference
from demos.iq.camera.processing import channelize, detect_emissions, direction_grid, locate


class CameraProcessingTests(unittest.TestCase):
    def test_algorithms_agree_on_single_source(self):
        rows, cols, na, ne, frequency = 2, 4, 128, 96, 2472e6
        grid = steering_vectors_2d(rows, cols, na, ne, frequency_hz=frequency, pixel_centers=True)
        target = (83, 39)
        a = grid[:, :, *target].ravel()
        covariance = np.outer(a, a.conj()) + 0.02 * np.eye(8)
        direct = np.einsum("rcae,rc->ae", grid.conj(), a.reshape(rows, cols))
        expected = np.unravel_index(np.argmax(abs(direct)), (na, ne))
        fft = bartlett_fft_2d(covariance, rows, cols, na, ne, frequency_hz=frequency)
        observed = np.unravel_index(np.argmax(fft), fft.shape)
        np.testing.assert_allclose(observed, expected, atol=1)
        directions, powers, purity = root_music_2d(covariance, rows, cols, frequency_hz=frequency, source_count=1)
        np.testing.assert_allclose(directions[0], [(2 * target[0] + 1) / na - 1, (2 * target[1] + 1) / ne - 1], atol=0.002)
        self.assertGreater(purity, 0.9)
        spectrum, _ = locate(covariance, direction_grid(rows, cols, frequency, na, ne), snapshots=200)
        np.testing.assert_allclose(np.unravel_index(np.argmax(spectrum), spectrum.shape), target, atol=1)

    def test_dc_rejection_and_separate_emissions(self):
        samples = np.ones((8, 4, 256), complex)
        self.assertEqual(np.max(abs(channelize(samples, np.ones(256), reject_dc=True))), 0)
        self.assertGreater(np.max(abs(channelize(samples, np.ones(256), reject_dc=False))), 0)
        spectra = np.zeros((8, 8, 32), complex)
        spectra[:, 1:3, 4:7] = 10
        spectra[:, 5:7, 20:23] = 8
        emissions = detect_emissions(spectra, np.ones(32), 6, np.ones(32, bool))
        self.assertEqual(len(emissions), 2)
        self.assertTrue(all(e.covariance.shape == (8, 8) for e in emissions))

    def test_sync_restores_narrow_filter_on_failure(self):
        calls = []
        board = SimpleNamespace(
            iq=SimpleNamespace(get_config=lambda: {"filter_bw_mhz": 20}, set_config=lambda config: calls.append(config))
        )
        with patch.object(reference, "_acquire_grid", side_effect=TimeoutError("no reference")):
            with self.assertRaises(TimeoutError):
                reference.acquire_grid(SimpleNamespace(boards=[board]), 2472e6, scope=SimpleNamespace(active=[0]))
        self.assertEqual(calls, [{"filter_bw_mhz": 40}, {"filter_bw_mhz": 20}])


if __name__ == "__main__":
    unittest.main()
