"""Restore a selected board independently of a controller-only reference master."""
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from espargos._iq_calibration.reference import reference_session
from espargos._iq_calibration.scope import CalibrationScope
from espargos.calibration import Antenna
from espargos.revisions import BoardRevisionDensiflorus


@pytest.mark.parametrize("fail", [False, True])
def test_scoped_reference_restores_board_and_master(fail):
    boards = [Mock(host=host, revision=BoardRevisionDensiflorus()) for host in ("master", "array")]
    master, array = boards
    master.control.get_json.return_value = {"mode": "wifi"}
    master.wifi_rx.get_config.return_value = {"calib-source": 1}
    selected = dict(antid=3, rf_freq_hz=2437000000, gain_mode="auto", rx_gain=54)
    modes = ["wifi"] * 8
    modes[3] = "iq_rx"
    config = dict(adc_decimation=2, rf_switch=3, trigger={"mode": "signal"},
                  receivers=[dict(selected, antid=a) for a in range(8)], modes=modes)
    array.iq.get_config.return_value = config
    array.control.get_json.return_value = {
        "muted": False, "sensors": [dict(enabled=False, applied_mode=mode) for mode in modes]}
    array.control.post_json.return_value = "ok"
    pool = SimpleNamespace(boards=boards, master_board=master, _reference_lock=Lock(), _calibrating=False)
    scope = CalibrationScope(pool, [Antenna("array", 3)])

    def acquire():
        with reference_session(pool, scope=scope) as (center, rate):
            assert (center, rate) == (2437000000, 40e6)
            assert pool._calibrating
            if fail:
                raise TimeoutError("reference disconnected")

    if fail:
        with pytest.raises(TimeoutError, match="reference disconnected"):
            acquire()
    else:
        acquire()
    assert not pool._calibrating
    master.iq.set_config.assert_not_called()
    assert master.iq.set_reference.call_count == 2
    assert master.control.post_json.call_args_list == [
        call("set_controller_mode", {"mode": "iq"}), call("set_controller_mode", {"mode": "wifi"})]
    assert array.iq.set_config.call_args.args[0] == dict(
        rf_switch=3, trigger=config["trigger"], adc_decimation=2, receivers=[selected])
    assert array.control.post_json.call_args_list == [
        call("set_iq_tx", {"muted": True}), call("set_iq_tx", {"muted": False})]
