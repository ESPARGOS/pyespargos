"""Board settings shared by monitor and reciprocity TX calibration."""

from ..iq_tx_control import _rpc
from .reference import _map


def snapshot(boards):
    rx = _map(lambda board: board.iq.get_config(), boards)
    tx = _map(lambda board: _rpc(board, "iq/tx"), boards)
    return rx, tx


def prepare_board(board, modes, antennas, bandwidth_mhz, *, silence=(), **settings):
    _rpc(board, "iq/monitor", {"enabled": False})
    transmitters = [{"antid": a, "rf_gain": {"a": 0, "b": 0}, "bandwidth_mhz": bandwidth_mhz} for a in antennas]
    transmitters.extend({"antid": a, "amplitude": 0} for a in silence)
    # RX-only boards still change mode. Firmware accepts an omitted list.
    if transmitters:
        settings["transmitters"] = transmitters
    _rpc(board, "iq/tx", {"muted": True, "modes": modes, **settings})


def restore_tx(board, saved, *, calibration=None):
    keys = ("antid", "frequency_hz", "rf_gain", "bandwidth_mhz", "output", "amplitude")
    settings = {
        "muted": True,
        "steering": saved["steering"],
        "transmitters": [{k: sensor[k] for k in keys} for sensor in saved["sensors"]],
    }
    if calibration is not None:
        settings["calibration"] = calibration
    _rpc(board, "iq/tx", settings)


def restore_monitor(board, saved):
    keys = ("enabled", "gain_mode", "frequency_hz", "sample_rate_hz", "gain", "bandwidth_mhz", "samples", "interval_us", "offset_us")
    _rpc(board, "iq/monitor", {k: saved[k] for k in keys})
