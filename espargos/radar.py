#!/usr/bin/env python

from dataclasses import dataclass

import numpy as np

from . import calibration
from . import constants

RADAR_TIME_SCALE = 1e6
FTM_TIMESTAMP_UNIT_S = 1.5625e-9


def ftm_get_phy_comp(
    *,
    responder: bool,
    primary_channel: int,
    secondary_channel: int = 0,
    mode: int = 0,
    sta_connected: bool = False,
) -> int:
    """
    Reimplementation of ESP32-C61 ``ftm_get_phy_comp`` from the Wi-Fi blob.

    This raw-units version is kept as a reference to the recovered original
    control flow. New code should prefer
    :func:`get_ftm_tx_timestamp_reciprocity_delay_s`, which expresses the same
    logic in a table-driven form and returns seconds directly.

    The returned value is in raw FTM timestamp units. Multiply by
    :data:`FTM_TIMESTAMP_UNIT_S` to convert to seconds. ``secondary_channel``
    follows ESP-IDF's ``wifi_second_chan_t`` convention: ``0`` none, ``1``
    above, ``2`` below.

    ``sta_connected`` only affects the initiator/nonzero-mode branch in the
    recovered implementation.
    """
    primary = int(primary_channel)
    secondary = int(secondary_channel) & 0xFF
    mode_nonzero = int(mode) != 0

    if not responder:
        if mode_nonzero:
            if sta_connected and primary > 10:
                return 0x3AD
            return 0x399

        if secondary == 1:
            if ((primary - 1) & 0xFF) <= 8:
                return 0x2EA if primary > 10 else 0x2EC
        elif secondary == 2:
            if ((primary - 5) & 0xFF) <= 8:
                return 0x2EA if primary > 10 else 0x2EC

        return 0x35C if primary > 10 else 0x361

    if mode_nonzero:
        return 0x23F if primary > 10 else 0x23E

    if secondary == 1:
        if primary == 0:
            return 0x363
        if primary <= 9:
            return 0x2EE
    elif secondary == 2:
        if ((primary - 5) & 0xFF) <= 8:
            return 0x2F4 if primary > 10 else 0x2EE

    return 0x365 if primary > 10 else 0x363


def _ftm_channel_group(primary_channel: int) -> str:
    """
    Group 2.4 GHz channels into the low and high ranges used by the blob logic.
    """
    return "high" if int(primary_channel) > 10 else "low"


def get_ftm_tx_timestamp_reciprocity_delay_s(
    *,
    responder: bool,
    primary_channel: int,
    secondary_channel: int = 0,
    home_channel_ht40: bool = False,
    mode: int | None = None,
    sta_connected: bool = False,
) -> float:
    """
    Return the PHY reciprocity delay used to align TX/RX FTM timestamps.

    This is the same compensation modeled by :func:`ftm_get_phy_comp`, but
    expressed in terms of role, HT40 home-channel state, channel layout, and
    channel group instead of reproducing the recovered branch tree. The returned
    value is in seconds.

    ``home_channel_ht40`` is intentionally separate from
    ``secondary_channel``. The blob does not use the nonzero ``mode`` branch as
    a generic "secondary channel present" test; it switches into a distinct PHY
    compensation regime used when the current/home channel context is HT40.
    That is why this flag remains separate even though ``primary_channel`` and
    ``secondary_channel`` are also provided.

    ``mode`` is kept as a backward-compatible alias for the recovered blob
    parameter. When provided, only its zero/nonzero state matters.

    ``secondary_channel`` follows ESP-IDF's ``wifi_second_chan_t`` convention:
    ``0`` none, ``1`` above, ``2`` below.
    """
    secondary = int(secondary_channel) & 0xFF
    if secondary == 1:
        layout = "ht40_above"
    elif secondary == 2:
        layout = "ht40_below"
    else:
        layout = "ht20"
    channel_group = _ftm_channel_group(primary_channel)
    use_ht40_home_channel_comp = bool(home_channel_ht40)
    if mode is not None:
        use_ht40_home_channel_comp = bool(mode)

    if not responder and use_ht40_home_channel_comp:
        ftm_units = 941 if sta_connected and channel_group == "high" else 921
    elif responder and use_ht40_home_channel_comp:
        ftm_units = {
            "low": 574,
            "high": 575,
        }[channel_group]
    elif not responder:
        ftm_units = {
            "ht20": {"low": 865, "high": 860},
            "ht40_above": {"low": 748, "high": 746},
            "ht40_below": {"low": 748, "high": 746},
        }[
            layout
        ][channel_group]
    else:
        ftm_units = {
            "ht20": {"low": 867, "high": 869},
            "ht40_above": {"low": 750, "high": 869},
            "ht40_below": {"low": 750, "high": 756},
        }[
            layout
        ][channel_group]

    return ftm_units * FTM_TIMESTAMP_UNIT_S


def ftm_get_phy_comp_s(**kwargs) -> float:
    """
    Backward-compatible wrapper returning the PHY compensation in seconds.
    """
    return get_ftm_tx_timestamp_reciprocity_delay_s(**kwargs)


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


def correct_radar_csi_tx_timestamps(
    csi_data: np.ndarray,
    tx_timestamps_s,
    tx_sensor_indices,
    subcarrier_frequencies_hz,
    calibration: calibration.CSICalibration,
    *,
    axis: int = -1,
    tx_timestamp_offset_s: float = 0.0,
    tx_timestamp_offsets_s=None,
    correction_sign: float = 1.0,
) -> np.ndarray:
    """
    Apply radar TX timestamp phase correction to frequency-domain CSI.

    The CSI deserialization path already applies the receive-side timestamp/STO
    correction. This helper applies the complementary transmit-side correction
    using the radar TX report timestamp. ``tx_timestamps_s`` are sensor-local TX
    timestamps; they are converted into the calibration reference clock by
    subtracting the transmitting sensor's clock offset.

    ``subcarrier_frequencies_hz`` may be absolute RF frequencies or baseband
    subcarrier offsets. For consistency with the existing CSI deserialization
    timestamp correction, callers usually want baseband offsets.

    :param csi_data: Complex CSI array. The subcarrier axis is selected by ``axis``.
    :param tx_timestamps_s: TX timestamp(s), in seconds. Scalar or leading-shape array.
    :param tx_sensor_indices: Flattened TX sensor index/indices matching ``tx_timestamps_s``.
    :param subcarrier_frequencies_hz: Frequency for each subcarrier, in Hz.
    :param calibration: Calibration object that provides sensor clock offsets.
    :param axis: Subcarrier axis in ``csi_data``.
    :param tx_timestamp_offset_s: Constant offset added to each TX timestamp
        before correction. This accounts for packet-boundary conventions in the
        hardware TX timestamp source.
    :param tx_timestamp_offsets_s: Optional per-TX-sensor offsets added to the
        corresponding TX timestamps, in seconds. This mirrors the FTM responder
        model where a calibrated offset is added to T1.
    :param correction_sign: Sign of the phase correction. The default matches the
        expected transmit-time inverse of the receive timestamp correction.
    :return: Corrected CSI array with the same shape as ``csi_data``.
    """
    csi_array = np.asarray(csi_data)
    frequencies = np.asarray(subcarrier_frequencies_hz, dtype=np.float64)
    if frequencies.ndim != 1:
        raise ValueError("subcarrier_frequencies_hz must be one-dimensional")

    csi_moved = np.moveaxis(csi_array, axis, -1)
    if csi_moved.shape[-1] != frequencies.shape[0]:
        raise ValueError("subcarrier_frequencies_hz length must match the CSI subcarrier axis")

    tx_timestamps = np.asarray(tx_timestamps_s, dtype=np.float64)
    tx_indices = np.asarray(tx_sensor_indices, dtype=np.int64)
    tx_timestamps, tx_indices = np.broadcast_arrays(tx_timestamps, tx_indices)

    flat_offsets = np.asarray(calibration.sensor_clock_offsets, dtype=np.float64).reshape(-1)
    per_tx_timestamp_offsets = np.zeros(flat_offsets.shape, dtype=np.float64)
    if tx_timestamp_offsets_s is not None:
        per_tx_timestamp_offsets = np.asarray(tx_timestamp_offsets_s, dtype=np.float64)
        if per_tx_timestamp_offsets.ndim == 0:
            per_tx_timestamp_offsets = np.full(flat_offsets.shape, float(per_tx_timestamp_offsets), dtype=np.float64)
        if per_tx_timestamp_offsets.shape != flat_offsets.shape:
            raise ValueError("tx_timestamp_offsets_s must be a scalar or match the flattened sensor count")

    tx_reference_timestamps = np.full(tx_timestamps.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(tx_timestamps) & (tx_indices >= 0) & (tx_indices < flat_offsets.size)
    valid_tx_indices = tx_indices[valid]
    tx_reference_timestamps[valid] = tx_timestamps[valid] + float(tx_timestamp_offset_s) + per_tx_timestamp_offsets[valid_tx_indices] - flat_offsets[valid_tx_indices]

    if tx_reference_timestamps.ndim > csi_moved.ndim - 1:
        raise ValueError("tx_timestamps_s has too many dimensions for csi_data")

    correction_shape = tx_reference_timestamps.shape + (1,) * (csi_moved.ndim - 1 - tx_reference_timestamps.ndim) + (frequencies.shape[0],)
    tx_reference_timestamps = tx_reference_timestamps.reshape(correction_shape[:-1])
    phase = correction_sign * 2.0 * np.pi * tx_reference_timestamps[..., np.newaxis] * frequencies.reshape((1,) * (len(correction_shape) - 1) + (-1,))
    corrected = csi_moved * np.exp(1.0j * phase).astype(csi_moved.dtype, copy=False)
    return np.moveaxis(corrected, -1, axis)
