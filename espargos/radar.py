#!/usr/bin/env python

from dataclasses import dataclass

import numpy as np

from . import calibration
from . import constants

RADAR_TIME_SCALE = 1e6


@dataclass
class RadarPoolConfig:
    """
    Low-level radar configuration for an entire pool, one controller config per board.
    """

    board_configs: list[dict]


def _normalize_per_antid(values, name: str, dtype=None):
    array = np.asarray(values if values is not None else [0] * constants.ANTENNAS_PER_BOARD, dtype=dtype)
    if array.ndim == 0:
        array = np.full(constants.ANTENNAS_PER_BOARD, array.item(), dtype=array.dtype)
    if array.shape != (constants.ANTENNAS_PER_BOARD,):
        raise ValueError(f"{name} must be a scalar or an array of length {constants.ANTENNAS_PER_BOARD}")
    return array


def _antid_to_row_col(board_revision, antid: int) -> tuple[int, int]:
    esp_num = board_revision.antid_to_esp_num[antid]
    return board_revision.esp_num_to_row_col(esp_num)


def _board_grid_from_antid_values(board_revision, values_by_antid: np.ndarray) -> np.ndarray:
    grid = np.zeros((constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW), dtype=values_by_antid.dtype)
    for antid, value in enumerate(values_by_antid):
        row, col = _antid_to_row_col(board_revision, antid)
        grid[row, col] = value
    return grid


def _board_antid_values_from_grid(board_revision, grid: np.ndarray) -> list:
    values = [None] * constants.ANTENNAS_PER_BOARD
    for antid in range(constants.ANTENNAS_PER_BOARD):
        row, col = _antid_to_row_col(board_revision, antid)
        values[antid] = grid[row, col].item() if hasattr(grid[row, col], "item") else grid[row, col]
    return values


def _default_mac_by_antid(board_index: int) -> list[str]:
    return [f"72:61:64:61:{board_index:02x}:{antid:02x}" for antid in range(constants.ANTENNAS_PER_BOARD)]


def _normalize_mac_by_board(mac_by_antid, board_count: int) -> list[list[str]]:
    if mac_by_antid is None:
        return [_default_mac_by_antid(board_index) for board_index in range(board_count)]

    if len(mac_by_antid) == board_count and all(isinstance(entry, (list, tuple)) for entry in mac_by_antid):
        macs = [[str(mac) for mac in board_macs] for board_macs in mac_by_antid]
        if any(len(board_macs) != constants.ANTENNAS_PER_BOARD for board_macs in macs):
            raise ValueError(f"Each board MAC list must have length {constants.ANTENNAS_PER_BOARD}")
        return macs

    if len(mac_by_antid) != constants.ANTENNAS_PER_BOARD:
        raise ValueError(f"mac_by_antid must either be a list of length {constants.ANTENNAS_PER_BOARD} " f"or a per-board list of {board_count} such lists")
    return [[str(mac) for mac in mac_by_antid] for _ in range(board_count)]


def build_pool_config(
    calibration: calibration.CSICalibration,
    active_by_antid,
    t0_by_antid,
    period_by_antid,
    tx_power: int,
    tx_phymode: int,
    tx_rate: int,
    rfswitch_state: int,
    mac_by_antid=None,
) -> RadarPoolConfig:
    """
    Build low-level per-board radar controller configuration from a reference-time schedule.

    ``t0_by_antid`` is interpreted as a reference time in seconds relative to sensor 0.
    The returned board configs contain ``start_by_antid`` values in controller units
    (currently integer microseconds), converted to each sensor's local clock using the
    stored ``sensor_clock_offsets`` from the provided calibration.
    """
    if calibration is None:
        raise ValueError("calibration must not be None")

    active_by_antid = _normalize_per_antid(active_by_antid, "active_by_antid", dtype=bool)
    t0_by_antid = _normalize_per_antid(t0_by_antid, "t0_by_antid", dtype=np.float64)
    period_by_antid = _normalize_per_antid(period_by_antid, "period_by_antid", dtype=np.float64)
    mac_by_board = _normalize_mac_by_board(mac_by_antid, len(calibration.boards))

    reference_times = np.zeros_like(calibration.sensor_clock_offsets, dtype=np.float64)
    for board_index, board in enumerate(calibration.boards):
        reference_times[board_index] = _board_grid_from_antid_values(board.revision, t0_by_antid)

    sensor_local_times = calibration.time_to_sensor_time(reference_times)

    board_configs = []
    period_us = np.rint(period_by_antid * RADAR_TIME_SCALE).astype(int).tolist()
    active_list = active_by_antid.astype(bool).tolist()
    for board_index, board in enumerate(calibration.boards):
        start_by_antid = _board_antid_values_from_grid(
            board.revision,
            np.rint(sensor_local_times[board_index] * RADAR_TIME_SCALE).astype(int),
        )
        board_configs.append(
            {
                "active_by_antid": active_list,
                "start_by_antid": start_by_antid,
                "period_by_antid": period_us,
                "mac_by_antid": mac_by_board[board_index],
                "rfswitch_state": int(rfswitch_state),
                "tx_power": int(tx_power),
                "tx_phymode": int(tx_phymode),
                "tx_rate": int(tx_rate),
            }
        )

    return RadarPoolConfig(board_configs=board_configs)
