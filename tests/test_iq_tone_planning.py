import unittest

import numpy as np

from espargos.iq_tone import tone_sweep_steps


class TonePlanningTests(unittest.TestCase):
    def test_calibration_at_2350_mhz(self):
        for fs in (80e6, 40e6, 20e6, 10e6, 8e6, 4e6):
            with self.subTest(fs=fs):
                steps = tone_sweep_steps(2350e6, fs)
                self.assertGreaterEqual(len(steps), 4)
                self.assertTrue(all(kind == "freq" for kind, _ in steps))
                frequencies = [khz * 1000 for _, khz in steps]
                self.assertEqual(len(set(frequencies)), len(frequencies))
                self.assertLess(min(frequencies), 2350e6)
                self.assertGreater(max(frequencies), 2350e6)
                self.assertTrue(all(abs(f - 2350e6) < 0.45 * fs for f in frequencies))

    def test_sweep_distinguishes_delays_across_fit_search_window(self):
        # Two delays anywhere in [-16, 16) must not have the same tone
        # signature after fitting a constant phase. The old 4 MHz grid
        # repeated after about 19.4 samples at 80 MSa/s.
        for fs in (80e6, 8e6):
            with self.subTest(fs=fs):
                frequencies = np.array([khz * 1000 for kind, khz in tone_sweep_steps(2350e6, fs) if kind == "freq"])
                delay_differences = np.arange(1.0, 32.0, 0.01)
                signatures = np.exp(2j * np.pi * np.outer(delay_differences, (frequencies - 2350e6) / fs))
                self.assertLess(np.max(np.abs(signatures.mean(axis=1))), 0.9)

    def test_exact_tones_stay_in_controller_range(self):
        for center in (2310e6, 2350e6, 2437e6, 2497e6):
            with self.subTest(center=center):
                steps = tone_sweep_steps(center, 80e6)
                self.assertTrue(steps)
                for kind, value in steps:
                    if kind == "freq":
                        self.assertGreaterEqual(value, 2310000)
                        self.assertLessEqual(value, 2497000)

    def test_high_band_still_uses_cap_tones(self):
        steps = tone_sweep_steps(2600e6, 80e6)
        self.assertTrue(steps)
        self.assertTrue(all(kind == "cap" for kind, _ in steps))


if __name__ == "__main__":
    unittest.main()
