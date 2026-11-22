"""Exercise both reference CW tuning paths through the receive API."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from espargos import iq_tone
from espargos.board_iq import IQCapability
from espargos.iq_receiver import IQReceiver


@pytest.fixture
def receiver():
    master = Mock()
    master.api_version = (5, 0)
    master.iq = IQCapability(master)
    rx = object.__new__(IQReceiver)
    rx._owner = SimpleNamespace(reference_controller=master, _reference_boards=set())
    return rx


@pytest.mark.parametrize("hz", [2310e6, 2350.125e6, 2437e6, 2497e6])
def test_exact_tone_frequency_sent_in_khz(receiver, hz):
    receiver.set_reference_tone(hz)
    receiver.master_board.control.post_json.assert_called_once_with(
        "set_iq_reference", {"mode": "cw", "freq_khz": round(hz / 1000), "backoff_qdb": 30})


@pytest.mark.parametrize("hz,approximate", [(2600e6, False), (2670e6, True)])
def test_high_band_uses_nearest_measured_cap(receiver, hz, approximate):
    cap, mhz, reported = receiver.place_highband_tone(hz, backoff_qdb=24)
    table = iq_tone.HIGHBAND_CAP_FREQ_EDGE_MHZ if approximate else iq_tone.HIGHBAND_CAP_FREQ_MHZ
    assert reported == approximate
    assert mhz == table[cap]
    assert abs(mhz - hz/1e6) == min(abs(f - hz/1e6) for f in table.values())
    receiver.master_board.control.post_json.assert_called_once_with("set_iq_reference", {
        "mode": "cw", "freq_khz": round(iq_tone.HIGHBAND_TONE_CODE_HZ/1000), "backoff_qdb": 24,
        "cw_tuning": {"cbw": iq_tone.HIGHBAND_TONE_CBW, "cap": cap}})
