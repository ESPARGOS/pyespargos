from unittest.mock import Mock, call

from espargos.control_legacy import LegacyControllerResources
from espargos._iq_calibration.tx_settings import prepare_board


def test_prepare_rx_only_board_without_transmitter_list():
    board = Mock()
    board._resources = LegacyControllerResources(board.control)
    board.control.post_json.return_value = "ok"
    modes = ["iq_rx"] * 8

    prepare_board(board, modes, [], bandwidth_mhz=60)

    assert board.control.post_json.call_args_list == [
        call("set_iq_monitor", {"enabled": False}),
        call("set_iq_tx", {"muted": True, "modes": modes}),
    ]


def test_prepare_only_adjusts_selected_pilots_and_silences_other_transmitters():
    board = Mock()
    board._resources = LegacyControllerResources(board.control)
    board.control.post_json.return_value = "ok"
    modes = ["iq_rx"] * 7 + ["iq_tx"]

    prepare_board(board, modes, [2], bandwidth_mhz=40, silence=[7])

    settings = board.control.post_json.call_args.args[1]
    assert settings["transmitters"] == [
        {"antid": 2, "rf_gain": {"a": 0, "b": 0}, "bandwidth_mhz": 40},
        {"antid": 7, "amplitude": 0},
    ]
