import numpy as np
import pytest

from espargos.calibration import Antenna, Calibration, CalibrationGroup, CalibrationState, AntennaCorrection
from espargos.iq_timing import TXRXDelayCalibration


def calibration():
    return Calibration(tuple(CalibrationGroup((AntennaCorrection(CalibrationState(Antenna("board", i), role, 2380e6, 80e6, 1, 1), 0, 0),)) for i, role in enumerate(("rx", "tx"))))


def response(delay):
    bins = np.fft.fftfreq(256) * 256
    h = np.exp(-2j * np.pi * bins * delay / 256)
    h[(bins % 2 != 0) | (abs(bins) > 96)] = 0
    return h


def test_reference_delay_is_removed_without_waveform_assumptions():
    result = TXRXDelayCalibration.from_reference(calibration(), measured_delay_s=52e-9, reference_delay_s=12e-9)
    assert list(result.delays_s.values()) == [pytest.approx(40e-9)]


@pytest.mark.parametrize("measured,reference", [(float("nan"), 0), (0, float("inf")), (0, -1)])
def test_invalid_reference_delay(measured, reference):
    with pytest.raises(ValueError):
        TXRXDelayCalibration.from_reference(calibration(), measured_delay_s=measured, reference_delay_s=reference)


def test_timing_requires_coherent_rx_and_tx_groups():
    with pytest.raises(ValueError):
        TXRXDelayCalibration.from_reference(calibration().select(direction="rx"), measured_delay_s=0)


def test_internal_probe_delay_fit():
    from espargos.iq_timing_capture import _delay

    for samples in (256, 1024):
        frequencies = np.fft.fftfreq(samples)
        for expected in (0.1, 80.23, samples - 0.02):
            h = np.exp(-2j * np.pi * frequencies * expected)
            measured = _delay(h, 80e6) * 80e6
            assert abs((measured - expected + samples / 2) % samples - samples / 2) < 0.003


def test_per_transmitter_delay_correction():
    cal = calibration()
    tx = cal.select(direction="tx").groups[0].corrections[0].state.antenna
    result = TXRXDelayCalibration(cal, {tx: 23e-9})
    f = np.array([-20e6, 0, 20e6])
    np.testing.assert_allclose(result.correction(tx, f) * np.exp(-2j * np.pi * f * 23e-9), 1)


def test_internal_probe_rejects_silence():
    from espargos.iq_timing_capture import _delay

    with pytest.raises(ValueError, match="No clear"):
        _delay(np.zeros(256), 80e6)


def test_timing_survives_split_groups_from_the_same_measurement():
    from dataclasses import replace

    cal = calibration()
    rx, tx = cal.groups
    extra = replace(rx.corrections[0], state=replace(rx.corrections[0].state, antenna=Antenna("board", 2)))
    rx = replace(rx, corrections=(*rx.corrections, extra))
    original = Calibration((rx, tx))
    timing = TXRXDelayCalibration.from_reference(original, measured_delay_s=10e-9)
    split = Calibration((replace(rx, corrections=rx.corrections[:1]), replace(rx, corrections=rx.corrections[1:]), tx))
    updated = timing.for_calibration(split)
    assert updated.delays_s == timing.delays_s
    assert len(updated.calibration.select(direction="rx").groups[0].corrections) == 2


def test_capture_cleanup_attempts_all_subscriptions():
    from espargos.iq_capture import _Captures
    from types import SimpleNamespace

    calls = []

    def unsubscribe(subscription):
        calls.append(subscription)
        if subscription == 2:
            raise RuntimeError("disconnected")

    capture = _Captures.__new__(_Captures)
    board = SimpleNamespace(unsubscribe_sensor_messages=unsubscribe)
    capture.subscriptions = [(board, 1), (board, 2)]
    with pytest.raises(ExceptionGroup, match="unsubscribe"):
        capture.close()
    assert calls == [2, 1]
    assert not capture.subscriptions


@pytest.mark.parametrize("path,delays,match", [("invalid", None, "Choose reference"), ("antenna_r", None, "requires reference_delays")])
def test_invalid_timing_route_does_not_touch_hardware(path, delays, match):
    from unittest.mock import Mock
    from espargos.iq_timing_capture import measure

    owner = Mock()
    with pytest.raises(ValueError, match=match):
        measure(owner, (), path, delays, 5)
    assert not owner.mock_calls


def test_timing_without_both_roles_does_not_change_capture_settings():
    from unittest.mock import Mock, patch
    from espargos.iq_timing_capture import measure

    board = Mock(host="board")
    board.iq.get_config.return_value = {"modes": ["iq_rx"] * 8}
    owner = Mock(boards=[board])
    with patch("espargos.iq_timing_capture._rpc", return_value={}):
        with pytest.raises(ValueError, match="needs running IQ receivers and transmitters"):
            measure(owner, [Antenna("board", 0)], "reference", None, 5)
    owner.refresh_calibration.assert_not_called()
    board.iq.set_config.assert_not_called()


def test_invalid_timing_path_delay_does_not_change_capture_settings():
    from unittest.mock import Mock, patch
    from espargos.iq_timing_capture import measure

    board = Mock(host="board")
    board.iq.get_config.return_value = {"modes": ["iq_tx", "iq_rx"] + ["wifi"] * 6}
    owner = Mock(boards=[board])
    tx, rx = Antenna("board", 0), Antenna("board", 1)
    with patch("espargos.iq_timing_capture._rpc", return_value={"sensors": [{"samples": 256}] * 8}):
        with pytest.raises(ValueError, match="finite and nonnegative"):
            measure(owner, [tx, rx], "antenna_r", {(tx, rx): -1e-9}, 5)
    owner.refresh_calibration.assert_not_called()
    board.iq.set_config.assert_not_called()


@pytest.mark.parametrize("restore_failure", [False, True])
def test_timing_failure_restores_every_board_and_keeps_tx_muted(restore_failure):
    from unittest.mock import Mock, patch
    from espargos.iq_timing_capture import measure

    boards = [Mock(host=host) for host in ("board", "other")]
    config = {"modes": ["iq_rx", "iq_tx"] + ["wifi"] * 6, "rf_switch": 2, "receivers": [], "trigger": {"mode": "off"}}
    boards[0].iq.get_config.return_value = config
    boards[1].iq.get_config.return_value = dict(config, modes=["wifi"] * 8)
    if restore_failure:
        boards[0].iq.set_config.side_effect = RuntimeError("restore failed")
    owner = Mock(boards=boards)
    owner.refresh_calibration.return_value = calibration()
    state = {"muted": False, "steering": {}, "sensors": [dict(antid=i, samples=256, output="antenna_r", amplitude=1, rf_gain={"a": 0, "b": 0}) for i in range(8)]}
    calls = []

    def rpc(board, path, payload=None):
        calls.append((board.host, path, payload))
        if path == "get_iq_tx":
            return state
        if payload == {"muted": True, "steering": {"phase_deg": [0] * 8, "delay_samples": [0] * 8}}:
            raise RuntimeError("prepare failed")

    with patch("espargos.iq_timing_capture._rpc", side_effect=rpc), patch("espargos.iq_timing_capture._Captures") as captures, patch("espargos.iq_timing_capture.compute_reference_path_delays", return_value=np.zeros((2, 2, 4))):
        for board in boards:
            board.revision.antenna_id_to_row_col.side_effect = lambda a: divmod(a, 4)
        with pytest.raises(ExceptionGroup if restore_failure else RuntimeError):
            measure(owner, [Antenna("board", 0), Antenna("board", 1)], "reference", None, 5)
        captures.return_value.close.assert_called_once()
    for board in boards:
        board.iq.set_config.assert_called_once_with({k: config[k] for k in ("rf_switch", "receivers", "trigger")})
        assert (board.host, "set_iq_tx", {"muted": True}) in calls
    assert not any(payload and payload.get("muted") is False for _, _, payload in calls)


def test_common_pilot_matches_one_sensor_comb_and_has_independent_arrays():
    from espargos.iq_tx_calibration_fit import pilots

    sensor_pilot = pilots(256)[0]
    common = pilots(256, mask=1)
    assert len(common) == 8
    for model in common:
        for expected, actual in zip(sensor_pilot, model):
            np.testing.assert_array_equal(actual, expected)
    for i in range(3):
        assert not np.shares_memory(common[0][i], common[1][i])
