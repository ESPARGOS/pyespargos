import unittest
from dataclasses import replace
import numpy as np
from espargos.calibration import *


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.state = CalibrationState(Antenna("a", 0), "tx", 2380e6, 80e6, 123, 456)
        self.c = AntennaCorrection(self.state, 0.3, 1e-9)
        self.result = Calibration((CalibrationGroup((self.c,)),))

    def test_stale_config_is_rejected(self):
        self.assertTrue(self.result.covers([self.state]))
        for change in [dict(direction="rx"), dict(frequency_hz=2437e6), dict(sample_rate_hz=40e6), dict(boot_id=124), dict(epoch=457), dict(settings=(("filter", 1),))]:
            state = replace(self.state, **change)
            self.assertFalse(self.result.covers([state]))
            with self.assertRaises(KeyError):
                self.result.correction(state)

    def test_independent_groups_and_subsets(self):
        other = replace(self.c, state=replace(self.state, antenna=Antenna("b", 3), frequency_hz=2437e6))
        rx = replace(self.c, state=replace(self.state, direction="rx"))
        combined = self.result.merge(Calibration((CalibrationGroup((other,)), CalibrationGroup((rx,)))))
        self.assertEqual(len(combined.groups), 3)
        selected = combined.select([Antenna("b", 3)], "tx", 2437e6)
        self.assertEqual(selected.groups[0].corrections, (other,))
        self.assertEqual(selected.groups[0].reference_id, combined.groups[1].reference_id)
        fresh = replace(self.c, state=replace(self.state, epoch=999))
        updated = combined.merge(Calibration((CalibrationGroup((fresh,)),)))
        self.assertFalse(updated.covers([self.state]))
        self.assertTrue(updated.covers([fresh.state, other.state, rx.state]))

    def test_duplicate_and_nonfinite_rejected(self):
        with self.assertRaises(ValueError):
            Calibration((self.result.groups[0],) * 2)
        with self.assertRaises(ValueError):
            replace(self.c, delay_s=float("nan"))

    def test_rx_model_matches_existing_correction(self):
        from espargos.sensor_calibration import SensorCalibration

        m = SensorCalibration((1, 2, 4), np.ones((1, 2, 4)) * 2e-9, np.ones((1, 2, 4)) * 0.3, 2380e6, reference_path_delays=np.ones((1, 2, 4)) * 1e-9)
        c = AntennaCorrection(replace(self.state, direction="rx"), -0.3 - 2 * np.pi * 2380e6 * 1e-9, -1e-9)
        f = np.array([2360e6, 2380e6, 2400e6])
        np.testing.assert_allclose(c.response(f), m.phase_time_correction(f)[0, 0, 0], atol=1e-7)


if __name__ == "__main__":
    unittest.main()
