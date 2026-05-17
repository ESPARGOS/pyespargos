#!/usr/bin/env python

from dataclasses import dataclass

import numpy as np

from . import calibration
from . import constants
from . import csi

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


@dataclass
class RadarPoolConfig:
    """
    Low-level radar configuration for an entire pool, one controller config per board.
    """

    board_configs: list[dict]


def _broadcast_to_pool_shape(values, name: str, boards, dtype=None):
    if values is None:
        return np.zeros(
            (len(boards), constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW),
            dtype=dtype,
        )

    array = np.asarray(values, dtype=dtype)
    if array.ndim == 0:
        return np.full(
            (len(boards), constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW),
            array.item(),
            dtype=array.dtype,
        )
    if array.shape == (constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW):
        return np.broadcast_to(array, (len(boards),) + array.shape).copy()
    if array.shape == (len(boards), constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW):
        return array

    raise ValueError(
        f"{name} must be a scalar, a (row, column) array, or a (board, row, column) array"
    )


def _default_mac_by_controller_antid(board_index: int) -> list[str]:
    return [f"72:61:64:61:{board_index:02x}:{antid:02x}" for antid in range(constants.ANTENNAS_PER_BOARD)]


def _macs_by_antids(mac_by_sensor, boards) -> list[list[str]]:
    board_count = len(boards)
    if mac_by_sensor is None:
        return [_default_mac_by_controller_antid(board_index) for board_index in range(board_count)]

    mac_array = np.asarray(mac_by_sensor, dtype=object)
    if mac_array.shape == (constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW):
        return [[str(mac) for mac in board.revision.sensor_values_to_antid_list(mac_array, name="mac_by_sensor")] for board in boards]
    if mac_array.shape == (board_count, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW):
        return [[str(mac) for mac in board.revision.sensor_values_to_antid_list(mac_array[board_index], name="mac_by_sensor")] for board_index, board in enumerate(boards)]

    raise ValueError("mac_by_sensor must be a (row, column) array or a (board, row, column) array")


def build_pool_config(
    calibration: calibration.CSICalibration,
    active_by_sensor,
    t0_by_sensor,
    period_by_sensor,
    tx_power: csi.wifi_tx_power_t,
    tx_phymode: csi.wifi_phy_mode_t,
    tx_rate: csi.wifi_phy_rate_t,
    rfswitch_state: csi.rfswitch_state_t,
    mac_by_sensor=None,
) -> RadarPoolConfig:
    """
    Build low-level per-board radar controller configuration from a reference-time schedule.

    ``active_by_sensor``, ``t0_by_sensor`` and ``period_by_sensor`` may be scalars,
    board-local ``(row, column)`` arrays, or pool-wide ``(board, row, column)`` arrays.

    ``t0_by_sensor`` is interpreted as a reference time in seconds relative to sensor 0.
    The returned board configs contain ``start_by_antid`` values in controller units
    (currently integer microseconds), converted to each sensor's local clock using the
    stored ``sensor_clock_offsets`` from the provided calibration.
    """
    if calibration is None:
        raise ValueError("calibration must not be None")

    active_by_sensor = _broadcast_to_pool_shape(active_by_sensor, "active_by_sensor", calibration.boards, dtype=bool)
    t0_by_sensor = _broadcast_to_pool_shape(t0_by_sensor, "t0_by_sensor", calibration.boards, dtype=np.float64)
    period_by_sensor = _broadcast_to_pool_shape(period_by_sensor, "period_by_sensor", calibration.boards, dtype=np.float64)
    mac_by_board = _macs_by_antids(mac_by_sensor, calibration.boards)
    sensor_local_times = calibration.time_to_sensor_time(t0_by_sensor)

    board_configs = []
    for board_index, board in enumerate(calibration.boards):
        start_by_antid = board.revision.sensor_values_to_antid_list(
            np.rint(sensor_local_times[board_index] * RADAR_TIME_SCALE).astype(int),
            name="t0_by_sensor",
        )
        board_configs.append(
            {
                "active_by_antid": board.revision.sensor_values_to_antid_list(active_by_sensor[board_index].astype(bool), name="active_by_sensor"),
                "start_by_antid": start_by_antid,
                "period_by_antid": board.revision.sensor_values_to_antid_list(np.rint(period_by_sensor[board_index] * RADAR_TIME_SCALE).astype(int), name="period_by_sensor"),
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
    sensor_clock_offsets,
    *,
    tx_timestamp_offset_s: float = 0.0,
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

    The subcarrier axis is expected to be the last axis of ``csi_data``.

    :param csi_data: Complex CSI array with subcarriers on the last axis.
    :param tx_timestamps_s: TX timestamp(s), in seconds. Scalar or leading-shape array.
    :param tx_sensor_indices: Flattened TX sensor index/indices matching ``tx_timestamps_s``.
    :param subcarrier_frequencies_hz: Frequency for each subcarrier, in Hz.
    :param sensor_clock_offsets: Sensor clock offsets, in seconds, as an array that
        flattens in the same order as ``tx_sensor_indices``.
    :param tx_timestamp_offset_s: Constant offset added to each TX timestamp
        before correction. This accounts for packet-boundary conventions in the
        hardware TX timestamp source.
    :return: Corrected CSI array with the same shape as ``csi_data``.
    """
    csi_array = np.asarray(csi_data)
    frequencies = np.asarray(subcarrier_frequencies_hz, dtype=np.float64)
    if frequencies.ndim != 1:
        raise ValueError("subcarrier_frequencies_hz must be one-dimensional")
    if csi_array.shape[-1] != frequencies.shape[0]:
        raise ValueError("subcarrier_frequencies_hz length must match the last CSI axis")

    tx_timestamps = np.asarray(tx_timestamps_s, dtype=np.float64)
    tx_indices = np.asarray(tx_sensor_indices, dtype=np.int64)
    tx_timestamps, tx_indices = np.broadcast_arrays(tx_timestamps, tx_indices)
    if tx_timestamps.ndim > csi_array.ndim - 1:
        raise ValueError("tx_timestamps_s has too many dimensions for csi_data")

    flat_offsets = np.asarray(sensor_clock_offsets, dtype=np.float64).reshape(-1)
    tx_reference_timestamps = np.full(tx_timestamps.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(tx_timestamps) & (tx_indices >= 0) & (tx_indices < flat_offsets.size)
    valid_tx_indices = tx_indices[valid]
    tx_reference_timestamps[valid] = tx_timestamps[valid] + float(tx_timestamp_offset_s) - flat_offsets[valid_tx_indices]

    leading_dims = csi_array.ndim - 1
    correction_shape = tx_reference_timestamps.shape + (1,) * (leading_dims - tx_reference_timestamps.ndim)
    tx_reference_timestamps = tx_reference_timestamps.reshape(correction_shape)
    phase = 2.0 * np.pi * tx_reference_timestamps[..., np.newaxis] * frequencies
    return csi_array * np.exp(1.0j * phase).astype(csi_array.dtype, copy=False)
