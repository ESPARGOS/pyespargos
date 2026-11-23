"""Validate the calibration against a second, independently acquired reference."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from espargos.iq_receiver import IQCalibrationError, IQReceiver
from espargos._iq_calibration import reference


@pytest.mark.parametrize("phase_change", [0, 0.8])
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
        return [SimpleNamespace(iq=samples[:, i : i + 256].reshape(1, 2, 4, 256), fire_time_ns=origin, grid_origin_ns=origin) for i in range(256, n - 256, 256)]

    receiver = object.__new__(IQReceiver)
    from espargos.board import Board

    board = Mock(spec=Board)
    board.is_stream_connected = False
    receiver._owner = SimpleNamespace(_transports=["websocket"])
    board.revision = Mock()
    board.revision.antenna_id_to_row_col.return_value = (0, 0)
    board.iq = Mock()
    receiver.boards = [board]
    receiver._logger = Mock()
    scope = SimpleNamespace(mask=np.ones((1, 2, 4), bool), pairs=[(0, 0)], active=[0], check_sync=lambda configs: False, has_grid=lambda configs: False)
    with (
        patch.object(reference, "reference_session", return_value=nullcontext((center, rate))),
        patch.object(reference, "acquire_grid", return_value=[1]),
        patch.object(reference, "collect", side_effect=[capture(0), capture(phase_change)]),
        patch("espargos.iq_receiver.compute_reference_path_delays", return_value=delays.reshape(1, 2, 4)),
    ):
        if phase_change:
            with pytest.raises(IQCalibrationError, match="validation residual"):
                receiver._sync(scope=scope, attempts=1)
            assert receiver.calibration is None
        else:
            model = receiver._sync(scope=scope, attempts=1)
            tones = center + np.array([-16e6, 0, 16e6])
            corrected = model.phase_time_correction(tones).reshape(8, -1) * np.exp(1j * phases[:, None])
            np.testing.assert_allclose(corrected, 1, atol=0.015)

    board.start.assert_called_once_with(transports=["websocket"])


def test_downsample_pilot_retry_switches_radio_and_trigger_together():
    from espargos._iq_calibration import stream

    center, rate = 2440000000, 16000
    board = Mock()
    board.is_stream_connected = True
    board.revision.antenna_id_to_row_col.return_value = (0, 0)
    config = dict(
        trigger=dict(mode="downsample", sample_rate_hz=rate),
        capture_path="bluetooth",
        bluetooth_filter_khz=1200,
    )
    board.iq.get_config.return_value = config.copy()
    receiver = object.__new__(IQReceiver)
    receiver.boards = [board]
    receiver._logger = Mock()
    scope = Mock(active=[0], pairs=[(0, 0)])
    scope.has_grid.return_value = True
    scope.check_sync.return_value = False

    def apply(patch):
        config.update(patch)
        assert config["trigger"]["mode"] != "downsample" or config["capture_path"] == "bluetooth"

    scope.apply_rx.side_effect = apply
    with (
        patch.object(reference, "reference_gain", return_value=30),
        patch.object(reference, "reference_session", return_value=nullcontext((center, rate))),
        patch.object(reference, "acquire_grid", return_value=[1]) as acquire,
        patch.object(stream, "measure_bluetooth", side_effect=[RuntimeError("first pilot lost"), RuntimeError("second pilot observed")]) as measure,
        pytest.raises(IQCalibrationError, match="second pilot observed"),
    ):
        receiver._sync(scope=scope, attempts=2)
    acquire.assert_called_once()
    assert [call.args[6] for call in measure.call_args_list] == [0, 1]
    assert config["capture_path"] == "bluetooth" and config["trigger"]["sample_rate_hz"] == rate
