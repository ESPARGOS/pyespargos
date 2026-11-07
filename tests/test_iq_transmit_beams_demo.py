import pathlib
import sys
import tempfile
import types
import unittest

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "demos"))
from common.iq_tx_waveform import load_iq, waveform
from espargos.steering import Beamspace
from espargos.waveforms import CW


def steering(positions, u, v, frequency, squint=False):
    az = np.degrees(np.arctan2(u, np.sqrt(1 - u * u - v * v)))
    el = np.degrees(np.arcsin(v))
    values = [Beamspace(az, el, squint).coefficients(p, frequency, CW(0)) for p in positions.reshape(-1, 2)]
    weights, delays = np.array(values).T
    shape = positions.shape[:-1]
    return np.rad2deg(np.angle(weights)).reshape(shape), delays.real.reshape(shape)


from espargos.array_geometry import ArrayGeometry as Layout
from espargos.revisions import BoardRevisionDensiflorus


class TransmitBeamTests(unittest.TestCase):
    def test_single_board_matches_firmware_steering_coordinates(self):
        board = types.SimpleNamespace(revision=BoardRevisionDensiflorus())
        pos = Layout(["localhost"]).positions([board])[0]
        expected = np.array([[-(a % 4 - 1.5), -0.5 if a < 4 else 0.5] for a in range(8)]) * 0.06
        np.testing.assert_allclose(pos, expected)
        phase, delay = steering(pos, 0.3, -0.2, 2380e6, True)
        np.testing.assert_allclose(np.exp(1j * np.deg2rad(phase)), np.exp(-2j * np.pi * 2380e6 * delay / 80e6))
        np.testing.assert_allclose(steering(pos, 0.3, -0.2, 2380e6)[1], 0)

    def test_kit_uses_global_positions_across_rotated_boards(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        layout = Layout(config=root / "config/aperture-kit-8x4.yml")
        boards = [types.SimpleNamespace(revision=BoardRevisionDensiflorus()) for _ in range(4)]
        pos = layout.positions(boards)
        self.assertEqual(len(np.unique(pos.reshape(-1, 2), axis=0)), 32)
        self.assertAlmostEqual(np.ptp(pos[..., 0]), 0.42)
        self.assertAlmostEqual(np.ptp(pos[..., 1]), 0.18)
        phase, _ = steering(pos, 0.2, 0, 2380e6)
        self.assertFalse(np.allclose(phase[0], phase[1]))

    def test_file_normalization_and_invalid_length(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "iq.npy"
            np.save(path, np.array([1 + 1j, -1 - 1j]))
            np.testing.assert_array_equal(load_iq(path, 2), [511 | (511 << 10), 513 | (513 << 10)])
            with self.assertRaises(ValueError):
                load_iq(path, 256)

    def test_chirp_fits_replay_buffer(self):
        b = dict(type="lfm", amplitude=0.1, start_mhz=-40, stop_mhz=40, duration_us=0.4, delay_us=1)
        recipe = waveform(b, 256)
        self.assertEqual(recipe["chirp_samples"], 32)
        self.assertEqual(recipe["offset_samples"], 80)
        with self.assertRaises(ValueError):
            waveform({**b, "delay_us": 4}, 256)


if __name__ == "__main__":
    unittest.main()
