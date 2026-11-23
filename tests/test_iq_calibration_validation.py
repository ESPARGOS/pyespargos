"""Validate the calibration against a second, independently acquired reference."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from espargos.iq_receiver import IQCalibrationError, IQReceiver
from espargos._iq_calibration import reference


@pytest.mark.parametrize("phase_change", [0, .8])
def test_held_out_broadband_reference(phase_change):
    rate, center = 80e6, 2444e6
    delays = np.linspace(0, 1.2e-9, 8)
    phases = np.linspace(0, 2.4, 8)
    rng = np.random.default_rng(91)

    def capture(change):
        n = 8192
        offsets = np.fft.fftfreq(n, 1 / rate)
        signal = np.fft.fft(rng.normal(size=n) + 1j * rng.normal(size=n))
        signal[np.abs(offsets) >= 18e6] = 0
        measured_phase = phases.copy()
        measured_phase[2] += change
        channel = np.exp(1j * measured_phase[:, None] - 2j * np.pi * delays[:, None] * (center + offsets))
        samples = np.fft.ifft(signal * channel)
        origin = np.zeros((1, 2, 4), dtype=np.uint64)
        return [SimpleNamespace(iq=samples[:, i:i+256].reshape(1, 2, 4, 256),
                                fire_time_ns=origin, grid_origin_ns=origin)
                for i in range(256, n - 256, 256)]

    receiver = object.__new__(IQReceiver)
    board = Mock()
    board.revision.antenna_id_to_row_col.return_value = (0, 0)
    receiver.boards = [board]
    receiver._logger = Mock()
    scope = SimpleNamespace(mask=np.ones((1, 2, 4), bool), pairs=[(0, 0)], active=[0], check_sync=lambda configs: False)
    with (patch.object(reference, "reference_session", return_value=nullcontext((center, rate))),
          patch.object(reference, "acquire_grid", return_value=[1]),
          patch.object(reference, "collect", side_effect=[capture(0), capture(phase_change)]),
          patch("espargos.iq_receiver.compute_reference_path_delays", return_value=delays.reshape(1, 2, 4))):
        if phase_change:
            with pytest.raises(IQCalibrationError, match="validation residual"):
                receiver._sync(scope=scope, attempts=1)
            assert receiver.calibration is None
        else:
            model = receiver._sync(scope=scope, attempts=1)
            tones = center + np.array([-16e6, 0, 16e6])
            corrected = model.phase_time_correction(tones).reshape(8, -1) * np.exp(1j * phases[:, None])
            np.testing.assert_allclose(corrected, 1, atol=.015)
