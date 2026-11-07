import unittest

import numpy as np

from espargos.iq_receiver import (
    _CALIBRATION_MAX_RESIDUAL_RAD,
    _remove_reference_path_term,
    _within_board_phase_residuals,
)


class IQCalibrationValidationTests(unittest.TestCase):
    def setUp(self):
        self.frequency_hz = 2.444e9
        self.reference_delays = np.linspace(0.0, 1.2e-9, 8).reshape(1, 2, 4)
        self.sensor_phases = np.asarray([[[0.0, 0.4, -1.1, 2.0], [-0.7, 1.4, -2.2, 0.9]]])
        cabled_correction = np.exp(-1j * self.sensor_phases)
        # SensorCalibration's normal correction is for OTA data.  It contains
        # the opposite of the reference-path term that the cabled probe needs.
        self.ota_correction = cabled_correction * np.exp(-2j * np.pi * self.reference_delays * self.frequency_hz)
        common = np.exp(1j * np.linspace(-1.0, 1.0, 12))[:, None, None, None]
        self.probe_spectra = common * np.exp(1j * self.sensor_phases)[None, ...]
        self.references = [(0, 1, 0)]

    def test_cabled_probe_flattens_all_sensor_phases(self):
        correction = _remove_reference_path_term(self.ota_correction, self.reference_delays, self.frequency_hz)
        residuals = _within_board_phase_residuals(self.probe_spectra, correction, self.references)
        np.testing.assert_allclose(residuals, 0.0, atol=1e-6)

    def test_bad_sensor_correction_exceeds_acceptance_limit(self):
        correction = _remove_reference_path_term(self.ota_correction, self.reference_delays, self.frequency_hz)
        correction = correction.copy()
        correction[0, 0, 2] *= np.exp(0.8j)
        residuals = _within_board_phase_residuals(self.probe_spectra, correction, self.references)
        self.assertAlmostEqual(residuals[0, 0, 2], 0.8, places=6)
        self.assertGreater(np.max(np.abs(residuals)), _CALIBRATION_MAX_RESIDUAL_RAD)


if __name__ == "__main__":
    unittest.main()
