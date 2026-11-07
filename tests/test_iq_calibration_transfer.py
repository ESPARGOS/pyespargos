from dataclasses import replace
import numpy as np
import pytest
from espargos.calibration import Antenna, Calibration, CalibrationGroup, CalibrationState, AntennaCorrection
from espargos.iq_timing import TXRXDelayCalibration


def model(direction="rx"):
    s = CalibrationState(Antenna("board", 0), direction, 2385e6, 80e6, 99, 1000, (("samples", 256),) if direction == "tx" else (), sample_origin_ns=100)
    return Calibration((CalibrationGroup((AntennaCorrection(s, 0.3, 2e-9),)),))


@pytest.mark.parametrize("direction", ["rx", "tx"])
def test_origin_transfer_cancels_measured_signal_change(direction):
    cal = model(direction)
    old = cal.groups[0].corrections[0]
    # A 37.5 ns physical change is truncated to a 37 ns timestamp difference.
    new = replace(old.state, epoch=1037, sample_origin_ns=137)
    changed = cal.retime([new])
    c = changed.correction(new)
    sign = 1 if direction == "rx" else -1
    f = np.fft.fftfreq(256, 1 / 80e6)
    measured = np.exp(sign * 2j * np.pi * f * 37.5e-9)
    np.testing.assert_allclose(measured * c.response(2385e6 + f), old.response(2385e6 + f), atol=1e-12)
    assert changed.groups[0].reference_id == cal.groups[0].reference_id
    assert old.state.epoch == 1000


@pytest.mark.parametrize("field,value", [("boot_id", 100), ("frequency_hz", 2386e6), ("sample_rate_hz", 40e6)])
def test_incompatible_state_is_not_reused(field, value):
    cal = model()
    s = cal.groups[0].corrections[0].state
    assert not cal.retime([replace(s, **{field: value})]).groups


def test_inactive_direction_kept_and_unknown_direction_not_invented():
    cal = model("rx")
    s = cal.groups[0].corrections[0].state
    tx = replace(s, direction="tx", epoch=2000)
    assert cal.retime([tx]) == cal
    assert not cal.retime([tx]).covers([tx])
    assert not cal.without([s.antenna]).groups


def test_rx_grid_wrap_does_not_add_fifty_seconds_of_delay():
    cal = model()
    s = cal.groups[0].corrections[0].state
    c = cal.retime([replace(s, epoch=3000, sample_origin_ns=100 + 53687091200)]).groups[0].corrections[0]
    assert c.delay_s == 2e-9


def test_tx_delay_reduced_modulo_replay_buffer():
    cal = model("tx")
    s = cal.groups[0].corrections[0].state
    c = cal.retime([replace(s, epoch=s.epoch + 3200000000 + 25)]).groups[0].corrections[0]
    assert c.delay_s == pytest.approx(2e-9 - 25e-9)


def test_timing_preserved_only_for_same_measured_groups():
    rx = model()
    tx = model("tx")
    cal = rx.merge(tx)
    timing = TXRXDelayCalibration(cal, {Antenna("board", 0): 123e-9})
    states = [replace(c.state, epoch=1037, sample_origin_ns=137) for g in cal.groups for c in g.corrections]
    updated = cal.retime(states)
    assert timing.for_calibration(updated).delays_s == timing.delays_s
    with pytest.raises(ValueError):
        timing.for_calibration(model().merge(model("tx")))
    with pytest.raises(ValueError):
        timing.for_calibration(updated.without([states[0].antenna]))


def test_pool_retune_discards_both_directions_before_restoring_frequency():
    from unittest.mock import patch
    from espargos import Pool
    import espargos.pool as api
    from test_pool import FakeBoard

    with patch.object(api, "Board", side_effect=FakeBoard), patch.object(api, "_rpc", return_value={"sensors": [{"frequency_hz": 2385e6}] * 8}), patch.object(api, "_wait"):
        pool = Pool("board").open()
        pool.boards[0].iq.config["modes"] = ["iq_rx"] * 8
        pool.boards[0].iq.config["receivers"][0]["rf_freq_hz"] = 2385e6
        pool.calibrations = model().merge(model("tx"))
        pool.select(antennas=[0]).configure(rx_gain=25)
        assert len(pool.calibrations.groups) == 2
        pool.select(antennas=[0]).configure(frequency_hz=2386e6)
        assert not pool.calibrations.groups
        pool.select(antennas=[0]).configure(frequency_hz=2385e6)
        assert not pool.calibrations.groups
        pool.close()


def test_pool_switch_keeps_active_frequency_and_both_directions():
    from unittest.mock import patch
    from espargos import Pool
    import espargos.pool as api
    from test_pool import FakeBoard

    with patch.object(api, "Board", side_effect=FakeBoard), patch.object(api, "_rpc", return_value={"sensors": [{"frequency_hz": 2385e6}] * 8}) as rpc, patch.object(api, "_wait"):
        pool = Pool("board").open()
        pool.boards[0].iq.config["modes"] = ["iq_rx"] * 8
        pool.boards[0].iq.config["receivers"][0]["rf_freq_hz"] = 2385e6
        pool.calibrations = model().merge(model("tx"))
        with patch.object(pool, "refresh_calibration") as refresh:
            pool.select(antennas=[0]).configure(mode="iq_tx")
            refresh.assert_called_once()
            change = next(c.args[2] for c in rpc.call_args_list if c.args[1] == "set_iq_tx")
            assert change["transmitters"][0]["frequency_hz"] == 2385e6
            assert change["transmitters"][0]["amplitude"] == 0
            assert len(pool.calibrations.groups) == 2
            pool.select(antennas=[0]).configure(mode="iq_rx")
            assert pool.boards[0].iq.config["receivers"][0]["rf_freq_hz"] == 2385e6
            pool.select(antennas=[0]).configure(mode="wifi")
            assert not pool.calibrations.groups
        pool.close()
