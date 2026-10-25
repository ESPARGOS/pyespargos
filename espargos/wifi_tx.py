"""WiFi transmission schedules and transmit-timestamp corrections."""

from dataclasses import dataclass

import numpy as np

from . import csi_calibration
from . import constants
from . import sensor
from . import wifi

__all__ = [
    "WIFI_TX_TIME_SCALE",
    "WiFiTxConfig",
    "build_tx_config",
    "correct_csi_tx_timestamps",
]

WIFI_TX_TIME_SCALE = 1e6


@dataclass
class WiFiTxConfig:
    """
    Low-level WiFi TX configuration for an entire pool, one controller config per board.
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

    raise ValueError(f"{name} must be a scalar, a (row, column) array, or a (board, row, column) array")


def _default_mac_by_controller_antenna_id(board_index: int) -> list[str]:
    return [f"72:61:64:61:{board_index:02x}:{antenna_id:02x}" for antenna_id in range(constants.ANTENNAS_PER_BOARD)]


def _macs_by_antenna_ids(mac_by_sensor, boards) -> list[list[str]]:
    board_count = len(boards)
    if mac_by_sensor is None:
        return [_default_mac_by_controller_antenna_id(board_index) for board_index in range(board_count)]

    mac_array = np.asarray(mac_by_sensor, dtype=object)
    if mac_array.shape == (constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW):
        return [[str(mac) for mac in board.revision.sensor_values_to_antenna_id_list(mac_array, name="mac_by_sensor")] for board in boards]
    if mac_array.shape == (board_count, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW):
        return [[str(mac) for mac in board.revision.sensor_values_to_antenna_id_list(mac_array[board_index], name="mac_by_sensor")] for board_index, board in enumerate(boards)]

    raise ValueError("mac_by_sensor must be a (row, column) array or a (board, row, column) array")


def build_tx_config(
    calibration: csi_calibration.CSICalibration,
    active_by_sensor,
    t0_by_sensor,
    period_by_sensor,
    tx_power: wifi.WiFiTxPower,
    tx_phy_mode: wifi.WiFiPhyMode,
    tx_rate: wifi.WiFiPhyRate,
    rf_switch_state: sensor.RFSwitchState,
    mac_by_sensor=None,
) -> WiFiTxConfig:
    """
    Build low-level per-board WiFi TX controller configuration from a reference-time schedule.

    ``active_by_sensor``, ``t0_by_sensor`` and ``period_by_sensor`` may be scalars,
    board-local ``(row, column)`` arrays, or pool-wide ``(board, row, column)`` arrays.

    With a pool-wide clock reference, ``t0_by_sensor`` is interpreted relative
    to sensor 0 of board 0. With independent per-board references, callers must
    provide one explicit time per board. The returned board configs contain
    ``start_by_antid`` values in controller units (currently integer
    microseconds), converted to each sensor's local clock using the stored
    ``timing_offsets`` from the provided calibration.
    """
    if calibration is None:
        raise ValueError("calibration must not be None")

    sensor_local_times = calibration.time_to_sensor_time(t0_by_sensor)
    active_by_sensor = _broadcast_to_pool_shape(active_by_sensor, "active_by_sensor", calibration.boards, dtype=bool)
    period_by_sensor = _broadcast_to_pool_shape(period_by_sensor, "period_by_sensor", calibration.boards, dtype=np.float64)
    mac_by_board = _macs_by_antenna_ids(mac_by_sensor, calibration.boards)

    board_configs = []
    for board_index, board in enumerate(calibration.boards):
        start_by_antenna_id = board.revision.sensor_values_to_antenna_id_list(
            np.rint(sensor_local_times[board_index] * WIFI_TX_TIME_SCALE).astype(int),
            name="t0_by_sensor",
        )
        board_configs.append(
            {
                "active_by_antid": board.revision.sensor_values_to_antenna_id_list(active_by_sensor[board_index].astype(bool), name="active_by_sensor"),
                "start_by_antid": start_by_antenna_id,
                "period_by_antid": board.revision.sensor_values_to_antenna_id_list(np.rint(period_by_sensor[board_index] * WIFI_TX_TIME_SCALE).astype(int), name="period_by_sensor"),
                "mac_by_antid": mac_by_board[board_index],
                "rfswitch_state": int(rf_switch_state),
                "tx_power": int(tx_power),
                "tx_phymode": int(tx_phy_mode),
                "tx_rate": int(tx_rate),
            }
        )

    return WiFiTxConfig(board_configs=board_configs)


def correct_csi_tx_timestamps(
    csi_data: np.ndarray,
    tx_timestamps_s,
    tx_sensor_indices,
    subcarrier_frequencies_hz,
    sensor_timing_offsets,
    *,
    tx_timestamp_offset_s: float = 0.0,
) -> np.ndarray:
    """
    Apply WiFi TX timestamp phase correction to frequency-domain CSI.

    The CSI deserialization path already applies the receive-side timestamp/STO
    correction. This helper applies the complementary transmit-side correction
    using the WiFi TX report timestamp. ``tx_timestamps_s`` are sensor-local TX
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
    :param sensor_timing_offsets: Per-sensor timing offsets, in seconds (e.g.
        :attr:`.SensorCalibration.timing_offsets`), as an array that
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

    flat_offsets = np.asarray(sensor_timing_offsets, dtype=np.float64).reshape(-1)
    tx_reference_timestamps = np.full(tx_timestamps.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(tx_timestamps) & (tx_indices >= 0) & (tx_indices < flat_offsets.size)
    valid_tx_indices = tx_indices[valid]
    tx_reference_timestamps[valid] = tx_timestamps[valid] + float(tx_timestamp_offset_s) - flat_offsets[valid_tx_indices]

    leading_dims = csi_array.ndim - 1
    correction_shape = tx_reference_timestamps.shape + (1,) * (leading_dims - tx_reference_timestamps.ndim)
    tx_reference_timestamps = tx_reference_timestamps.reshape(correction_shape)
    phase = 2.0 * np.pi * tx_reference_timestamps[..., np.newaxis] * frequencies
    return csi_array * np.exp(1.0j * phase).astype(csi_array.dtype, copy=False)
