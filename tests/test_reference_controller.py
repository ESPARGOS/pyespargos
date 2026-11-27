from contextlib import ExitStack
from unittest.mock import Mock, patch

import pytest

from espargos.control_legacy import LegacyControllerResources
from espargos import Board, Pool
from espargos.controller_reference import prepare_packet_reference


def test_controller_only_master_is_outside_every_sensor_selection():
    config = {
        "boards": {"left": {"host": "left"}, "right": {"host": "right"}},
        "array": [[f"{board}.{r}.{c}" for board in ("left", "right") for c in range(4)] for r in range(2)],
        "reference_controller": "master",
    }
    boards = {name: Mock(spec=Board, host=name) for name in ("left", "right", "master")}
    for b in boards.values():
        b.api_version = (5, 0)
        b._resources = LegacyControllerResources(Mock())
        b.revision = Mock()
        b.revision.antenna_id_to_row_col.side_effect = lambda a: divmod(a, 4)
        b.iq, b.ble, b.wifi_iq, b.wifi_rx, b.wifi_tx = (Mock() for _ in range(5))
        b.iq.get_config.return_value = {"modes": ["wifi", "wifi_iq", "ble", "iq_rx", "iq_tx", "wifi", "wifi", "wifi"]}
    with patch("espargos.pool.Board", side_effect=boards.__getitem__), Pool.from_config(config) as pool:
        master = boards["master"]
        assert pool.reference_controller is master
        assert pool.iq.master_board is master and pool.iq.master_index is None
        assert {a.host for a in pool.antennas} == {"left", "right"}
        assert pool.indexing_matrix.shape == (2, 8)
        assert pool.reference_cables["cable_lengths"].shape == (2,)
        for receiver, count in [(pool.wifi, 8), (pool.wifi_iq, 2), (pool.ble, 2), (pool.iq, 2)]:
            assert receiver.boards == pool.boards == [boards["left"], boards["right"]]
            assert receiver._expected.sum() == count
        with patch("espargos.configuration._wait"):
            pool.configure(mode="ble")
        master.start.assert_not_called()
        assert not any(cap.mock_calls for cap in (master.iq, master.ble, master.wifi_iq, master.wifi_rx, master.wifi_tx))
    master.close.assert_called_once()
    with pytest.raises(ValueError, match="distinct"):
        Pool(["left", "right"], reference_controller="left")


@pytest.mark.parametrize("mode", ["wifi", "wifi_iq", "ble"])
def test_packet_master_restored_when_acquisition_fails(mode):
    board = Mock(host="master")
    board._resources = LegacyControllerResources(board.control)
    board.control.get_json.return_value = {"mode": "iq"}
    radio = board.wifi_rx if mode == "wifi" else getattr(board, mode)
    original = {"calib-mode": 1, "channel-primary": 1, "channel-secondary": 0} if mode == "wifi" else {"channel": 1, "reference_mode": 1, "rf_switch": 2, "reference_address": "02:00:00:00:00:01", "phy": "LE2M"}
    state = original.copy()
    radio.get_config.side_effect = state.copy
    write = radio.set_reference_config if mode == "wifi" else radio.set_config
    write.side_effect = state.update
    with pytest.raises(TimeoutError), ExitStack() as cleanup:
        address = prepare_packet_reference(cleanup, board, mode, 6)
        assert board.control.post_json.call_args.args == ("set_controller_mode", {"mode": mode})
        if mode == "wifi":
            assert state["channel-primary"] == 6 and state["calib-mode"] == 2
        else:
            assert state["channel"] == 6 and state["rf_switch"] == 1
            assert address == original["reference_address"]
        raise TimeoutError
    assert state == original
    board.control.post_json.assert_called_with("set_controller_mode", {"mode": "iq"})
