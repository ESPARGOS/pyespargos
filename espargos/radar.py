#!/usr/bin/env python

import time
import warnings
from dataclasses import dataclass

import numpy as np

from . import calibration
from . import constants
from . import csi
from . import util

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

    raise ValueError(f"{name} must be a scalar, a (row, column) array, or a (board, row, column) array")


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


# ---------------------------------------------------------------------------
# High-level, pool-wide radar setup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RadarFormat:
    """
    Transmit-side description of a radar waveform: the PHY mode and rate to send.

    This is intentionally TX-only. The receive side is decoupled: the receiver's
    default CSI acquire config already accepts every mode, and the format of a
    received packet is detected/deserialized independently (see
    :func:`deserialize_rx_csi`). :class:`RadarSession` never changes RX acquire
    settings.
    """

    tx_phymode: int
    tx_rate: int


#: Built-in radar TX waveforms keyed by short name (matches the RX preamble-format names).
RADAR_FORMATS = {
    "lltf": RadarFormat(
        tx_phymode=csi.wifi_phy_mode_t.WIFI_PHY_MODE_11G,
        tx_rate=csi.wifi_phy_rate_t.WIFI_PHY_RATE_6M,
    ),
    "ht20": RadarFormat(
        tx_phymode=csi.wifi_phy_mode_t.WIFI_PHY_MODE_HT20,
        tx_rate=csi.wifi_phy_rate_t.WIFI_PHY_RATE_MCS0_LGI,
    ),
    "ht40": RadarFormat(
        tx_phymode=csi.wifi_phy_mode_t.WIFI_PHY_MODE_HT40,
        tx_rate=csi.wifi_phy_rate_t.WIFI_PHY_RATE_MCS0_LGI,
    ),
    "he20": RadarFormat(
        tx_phymode=csi.wifi_phy_mode_t.WIFI_PHY_MODE_HE20,
        tx_rate=csi.wifi_phy_rate_t.WIFI_PHY_RATE_MCS0_LGI,
    ),
}


#: RX LTF formats in auto-detection priority order, mapped to the cluster/calibration
#: methods used to test availability, deserialize, and calibrate them.
RX_FORMATS = ("lltf", "ht20", "ht40", "he20")
_RX_DISPATCH = {
    "lltf": ("has_lltf", "deserialize_csi_lltf", "apply_lltf"),
    "ht20": ("has_ht20ltf", "deserialize_csi_ht20ltf", "apply_ht20"),
    "ht40": ("has_ht40ltf", "deserialize_csi_ht40ltf", "apply_ht40"),
    "he20": ("has_he20ltf", "deserialize_csi_he20ltf", "apply_he20"),
}


def interpolate_rx_gap(values: np.ndarray, fmt: str) -> None:
    """
    Fill an RX format's null / DC gap subcarriers in place (no-op for lltf).

    Interpolation is only meaningful once the per-subcarrier phase slope (STO /
    residual delay) has been removed — otherwise the complex average across the
    gap sits between two rotated phasors and collapses into a magnitude notch.
    Callers that still carry a large delay slope (e.g. before the transmit-side
    timestamp correction) should interpolate afterwards, not here.
    """
    if fmt == "ht40":
        csi.interpolate_ht40ltf_gap(values)
    elif fmt == "ht20":
        csi.interpolate_ht20ltf_gap(values)
    elif fmt == "he20":
        csi.interpolate_he20ltf_gaps(values)


def deserialize_rx_csi(cluster, calibration, fmt="auto", interpolate=True):
    """
    Deserialize and calibrate a radar cluster's CSI in a format-agnostic way.

    ``fmt`` is one of :data:`RX_FORMATS` or ``"auto"`` (detect the LTF format
    actually present in this cluster). Returns ``(resolved_format, csi)`` where
    ``csi`` is the calibrated CSI array, or ``(None, None)`` if the requested/any
    known LTF is not present. This does not change the pool's acquire config — TX
    and RX are decoupled.

    When ``interpolate`` is true the null / DC gap subcarriers are filled via
    :func:`interpolate_rx_gap` (ht20/ht40/he20). Pass ``interpolate=False`` if you
    apply a delay/STO correction downstream and want to interpolate the gap only
    after the phase has been flattened.
    """
    if fmt == "auto":
        fmt = next((f for f in RX_FORMATS if getattr(cluster, _RX_DISPATCH[f][0])()), None)
        if fmt is None:
            return None, None
    has_method, deserialize_method, apply_method = _RX_DISPATCH[fmt]
    if not getattr(cluster, has_method)():
        return None, None
    values = getattr(calibration, apply_method)(getattr(cluster, deserialize_method)())
    if interpolate:
        interpolate_rx_gap(values, fmt)
    return fmt, values


def rx_subcarrier_spacing(fmt: str) -> float:
    """Subcarrier spacing in Hz for an RX LTF format (HE20 uses quarter spacing)."""
    return constants.WIFI_SUBCARRIER_SPACING / 4.0 if fmt == "he20" else constants.WIFI_SUBCARRIER_SPACING


def rx_acquire_config(fmt: str) -> dict:
    """
    CSI acquire config for an RX preamble format selection.

    ``"auto"`` (or ``None``) returns the receiver default that accepts every LTF
    format — good for any TX waveform, and what auto-detection needs. A specific
    format forces/narrows acquisition to it. This is an RX-side concern applied by
    the demo/RX layer; :class:`RadarSession` never applies it (TX/RX are decoupled).
    """
    if fmt in (None, "auto"):
        return {
            "acquire_csi_force_lltf": False,
            "acquire_csi_legacy": True,
            "acquire_csi_ht20": True,
            "acquire_csi_ht40": True,
            "acquire_csi_su": True,
        }
    if fmt == "lltf":
        return {"acquire_csi_force_lltf": True}
    if fmt == "ht20":
        return {"acquire_csi_force_lltf": False, "acquire_csi_ht20": True}
    if fmt == "ht40":
        return {"acquire_csi_force_lltf": False, "acquire_csi_ht40": True}
    if fmt == "he20":
        return {"acquire_csi_force_lltf": False, "acquire_csi_su": True}
    raise ValueError(f"unknown RX format {fmt!r}")


def rx_subcarrier_frequencies(calibration, fmt: str) -> np.ndarray:
    """
    Baseband subcarrier frequencies (Hz, relative to the receiver LO) for an RX LTF format.

    ``calibration.channel_secondary`` is a *relative* secondary-channel flag (0 = none,
    +1 = above, -1 = below), not a channel number. In a 40 MHz channel the receiver LO sits
    ``rel * 2 * WIFI_CHANNEL_SPACING`` (~10 MHz) off the primary 20 MHz center, so the 20 MHz
    preambles (lltf/ht20/he20) are offset by that amount. Referencing the LO here keeps the
    transmit-side timestamp correction consistent with the receive-side STO correction in
    :meth:`CSICluster.deserialize_csi_lltf` / ``deserialize_csi_ht20ltf`` (which shift the
    subcarrier range by ``rel * 2*WIFI_CHANNEL_SPACING/WIFI_SUBCARRIER_SPACING`` subcarriers).
    In a single (20 MHz) channel ``rel == 0``, so this is a no-op. HT40 already spans the full
    40 MHz centered on the LO (no shift on either side).
    """
    primary = calibration.channel_primary
    secondary_relative = getattr(calibration, "channel_secondary", 0) or 0
    lo_offset = secondary_relative * 2 * constants.WIFI_CHANNEL_SPACING
    if fmt == "lltf":
        return util.get_frequencies_lltf(primary) - util.get_center_frequency(primary) - lo_offset
    if fmt == "ht20":
        return util.get_frequencies_ht20(primary) - util.get_center_frequency(primary) - lo_offset
    if fmt == "he20":
        return util.get_frequencies_he20(primary) - util.get_center_frequency(primary) - lo_offset
    if fmt == "ht40":
        return csi.get_csi_format_subcarrier_indices("ht40") * constants.WIFI_SUBCARRIER_SPACING
    raise ValueError(f"unknown RX format {fmt!r}")


def radar_completion_predicate(completion="all", rx_boards=None):
    """
    Build a ``cb_predicate`` selecting complete radar packets (no session needed).

    ``completion`` is ``"all"`` (every antenna except the transmitting one received),
    ``"any"`` (at least one sensor received), or an integer minimum received-sensor count.

    ``rx_boards`` optionally scopes the completion check to a subset of receiver arrays
    (a board index, list of indices, or slice into the ``(board, row, column)`` completion
    array). This is what makes bistatic setups work: with the transmitter on a different
    array, the whole pool is never complete, but the receiver array is.
    """

    def pred(cluster):
        if not (cluster.is_radar() and cluster.has_radar_tx_report()):
            return False
        comp = np.asarray(cluster.get_completion())
        if rx_boards is not None:
            comp = comp[rx_boards]
        if completion == "all":
            return np.sum(comp) >= comp.size - 1
        if completion == "any":
            return bool(np.any(comp))
        return np.count_nonzero(comp) >= int(completion)

    return pred


@dataclass
class RadarConfig:
    """
    Generic, pool-wide radar transmit/receive settings.

    A high-level description of *what* the radar should do; turning it into the
    low-level per-board controller configuration is the job of
    :class:`RadarSession`.

    :param tx_antennas: Which antennas transmit. Accepts ``True`` (all antennas),
        a single pool-wide sensor index, a sequence of pool-wide sensor indices, or
        a boolean numpy array shaped ``(row, column)``, ``(board, row, column)`` or
        flattenable to those. Integer indices are pool-wide flat indices
        (``board * 8 + row * 4 + col``, matching ``cluster.get_radar_tx_index()``),
        so a specific antenna on a specific array can be selected for bistatic setups;
        boolean ``(row, column)`` masks still apply to every board.
    :param interval: Transmit interval (period) of each transmitting antenna, in
        seconds.
    :param format: Radar data format, one of :data:`RADAR_FORMATS` (``"lltf"``,
        ``"ht20"``, ``"ht40"``, ``"he20"``).
    :param tx_power: Transmit power (:class:`csi.wifi_tx_power_t` or raw int).
    :param rfswitch_state: RF switch state during radar mode
        (:class:`csi.rfswitch_state_t` or raw int).
    :param cfo_compensation: Whether the receiver applies automatic CFO
        correction. Disabled by default for radar: TX and RX share a reference
        clock, so forcing the frequency-offset estimate to zero removes
        packet-to-packet phase noise.
    :param stagger: When multiple antennas transmit, spread their start times
        uniformly across ``interval`` so only one transmits at a time (TDM).
        Ignored when ``slot`` is set.
    :param start: Optional explicit reference start time for the first antenna, in
        seconds. Clamped up to a safe minimum derived from the sensor clock
        offsets. When ``None`` the safe minimum is used.
    :param slot: Optional explicit per-antenna start-time spacing, in seconds. When
        set, antenna ``i`` (row-major sensor index) starts at ``start + i * slot``
        on every board, reproducing a configurable start/slot schedule. Overrides
        ``stagger``.
    :param tx_timestamp_offset_ns: Constant TX-timestamp offset used when
        correcting radar CSI (hardware packet-boundary convention).
    :param tx_phymode: Optional override of the format's TX PHY mode.
    :param tx_rate: Optional override of the format's TX rate.
    :param mac_by_sensor: Optional per-sensor source-MAC override.
    :param callsign: Optional radar packet identifier / callsign (experimental;
        only sent when non-empty).
    """

    tx_antennas: object = True
    interval: float = 0.01
    format: str = "lltf"
    tx_power: int = csi.wifi_tx_power_t.WIFI_TX_POWER_2_DBM
    rfswitch_state: int = csi.rfswitch_state_t.SENSOR_RFSWITCH_ANTENNA_R
    cfo_compensation: bool = False
    stagger: bool = True
    start: float = None
    slot: float = None
    tx_timestamp_offset_ns: float = 1085.0
    tx_phymode: int = None
    tx_rate: int = None
    mac_by_sensor: object = None
    callsign: str = ""


class RadarSession:
    """
    Drives radar transmission/reception for a whole pool from a :class:`RadarConfig`.

    Typical use::

        session = espargos.radar.RadarSession(pool, espargos.radar.RadarConfig(
            tx_antennas=3, interval=4.882e-3, format="lltf"))
        session.configure()                  # RX format + CFO policy + TX schedule
        session.calibrate_gains(drive=True)  # optional AGC gain lock (single-TX only)
        pool.add_csi_callback(cb, cb_predicate=session.predicate())
        ...
        session.stop()                       # disable transmission

    Two transmit modes matter for gain handling:

    * Single TX antenna: the received power is constant packet-to-packet, so
      :meth:`calibrate_gains` can freeze per-sensor gains. The schedule is trivial
      (one antenna at one period).
    * Multiple TX antennas (TDM): the schedule staggers antennas across ``interval``
      and the per-slot RX power varies, so gains must stay on AGC —
      :meth:`calibrate_gains` refuses to run and callers keep automatic gain control.
    """

    def __init__(self, pool, config: "RadarConfig" = None):
        self.pool = pool
        self.config = config if config is not None else RadarConfig()

    @property
    def format(self) -> RadarFormat:
        try:
            return RADAR_FORMATS[self.config.format]
        except KeyError:
            raise ValueError(f"unknown radar format {self.config.format!r}, expected one of {sorted(RADAR_FORMATS)}")

    @property
    def tx_timestamp_offset_s(self) -> float:
        return self.config.tx_timestamp_offset_ns * 1e-9

    def _calibration(self):
        calib = self.pool.get_calibration()
        if calib is None:
            raise RuntimeError("pool is not calibrated yet; call pool.calibrate() first")
        return calib

    def _active_mask(self, calib) -> np.ndarray:
        """Normalize ``tx_antennas`` to a ``(board, row, column)`` boolean array."""
        shape = (len(calib.boards), constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)
        antennas = self.config.tx_antennas
        if isinstance(antennas, bool):
            return np.full(shape, antennas, dtype=bool)
        if isinstance(antennas, (int, np.integer)):
            antennas = [int(antennas)]
        arr = np.asarray(antennas)
        if arr.dtype == bool:
            if arr.shape == (constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW):
                return np.broadcast_to(arr, shape).copy()
            if arr.shape == shape:
                return arr.astype(bool)
            if arr.size == constants.ANTENNAS_PER_BOARD:
                grid = arr.reshape(constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)
                return np.broadcast_to(grid, shape).copy()
            raise ValueError(f"boolean tx_antennas mask has unexpected shape {arr.shape}")
        # Sequence of pool-wide flat sensor indices: board*ANTENNAS_PER_BOARD + row*ANTENNAS_PER_ROW + col
        # (matches cluster.get_radar_tx_index()), so a specific antenna on a specific array can be selected
        # for bistatic setups. For a single-board pool this is just antenna 0..7 on board 0.
        active = np.zeros(shape, dtype=bool)
        active.reshape(-1)[np.asarray(arr, dtype=int).reshape(-1)] = True
        return active

    def _min_safe_start(self, calib) -> float:
        return max(0.0, -float(np.nanmin(calib.sensor_clock_offsets))) + 1e-6

    def num_tx_antennas(self) -> int:
        """Number of antennas that transmit in the current configuration."""
        return int(np.count_nonzero(self._active_mask(self._calibration())))

    def _schedule(self, calib):
        """Return ``(active, t0, period)`` arguments for :func:`build_pool_config`."""
        active = self._active_mask(calib)
        base = self._min_safe_start(calib)
        start = base if self.config.start is None else max(float(self.config.start), base)
        if self.config.slot is not None:
            # Explicit configurable schedule: antenna i starts at start + i*slot on every board.
            index_grid = np.arange(constants.ANTENNAS_PER_BOARD).reshape(constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)
            t0_grid = start + index_grid * float(self.config.slot)
            t0 = np.broadcast_to(t0_grid, active.shape).copy()
        elif self.config.stagger:
            # Time-division multiplex: spread active antennas evenly across one interval.
            positions = np.argwhere(active)
            if len(positions) > 0:
                t0 = np.full(active.shape, start, dtype=np.float64)
                for slot_index, (b, r, c) in enumerate(positions):
                    t0[b, r, c] = start + slot_index * self.config.interval / len(positions)
            else:
                t0 = start
        else:
            t0 = start
        return active, t0, float(self.config.interval)

    def build_pool_config(self) -> RadarPoolConfig:
        """Build the low-level :class:`RadarPoolConfig` for the current settings."""
        calib = self._calibration()
        active, t0, period = self._schedule(calib)
        fmt = self.format
        pool_config = build_pool_config(
            calibration=calib,
            active_by_sensor=active,
            t0_by_sensor=t0,
            period_by_sensor=period,
            tx_power=self.config.tx_power,
            tx_phymode=fmt.tx_phymode if self.config.tx_phymode is None else self.config.tx_phymode,
            tx_rate=fmt.tx_rate if self.config.tx_rate is None else self.config.tx_rate,
            rfswitch_state=self.config.rfswitch_state,
            mac_by_sensor=self.config.mac_by_sensor,
        )
        if self.config.callsign:
            for board_config in pool_config.board_configs:
                board_config["callsign"] = self.config.callsign
        return pool_config

    def configure(self):
        """
        Apply the CFO-correction policy and TX schedule to the pool.

        TX only: this never touches the RX CSI acquire config. The receiver's
        default acquire config already accepts every mode, and received CSI is
        deserialized independently via :func:`deserialize_rx_csi`.
        """
        self.pool.set_cfo_correction(auto=self.config.cfo_compensation, value=0)
        self.pool.set_radar_config(self.build_pool_config())

    def predicate(self, completion="all", rx_boards=None):
        """
        Build a ``cb_predicate`` selecting complete radar packets.

        ``completion`` is ``"all"`` (every antenna except the transmitting one
        received), ``"any"`` (at least one sensor received), or an integer minimum
        received-sensor count. ``rx_boards`` optionally scopes the check to a subset
        of receiver arrays (see :func:`radar_completion_predicate`). Equivalent to the
        standalone function (usable without a session).
        """
        return radar_completion_predicate(completion, rx_boards=rx_boards)

    def calibrate_gains(self, duration=1.0, *, drive=False, settle=0.5, fallback=None, predicate=None):
        """
        Run automatic gain control briefly, then lock the converged per-sensor gains.

        This only makes sense with a SINGLE transmitting antenna: freezing one manual
        RX gain per sensor is valid only when every packet comes from the same TX
        antenna, so the received power stays constant. With multiple TX antennas
        (TDM) the per-slot RX power varies, so gain freezing is refused — keep AGC
        enabled instead. Calling this with more than one active TX antenna raises
        :class:`ValueError`.

        The radar must already be configured (see :meth:`configure`). Set
        ``drive=True`` to have this method pump ``pool.run()`` for ``duration``
        seconds; leave it ``False`` when the pool is already being driven on
        another thread. If no radar packets arrive, ``fallback`` gain settings are
        applied when given, otherwise a :class:`RuntimeError` is raised.

        :returns: The locked gain-settings dict, or ``None`` if the fallback was used.
        """
        n_tx = self.num_tx_antennas()
        if n_tx != 1:
            raise ValueError(
                f"gain calibration freezes one manual RX gain per sensor, which is only valid with a single "
                f"transmitting antenna (constant per-packet RX power); {n_tx} antennas are active. Keep AGC "
                f"enabled for multi-antenna TDM radar instead of calling calibrate_gains()."
            )
        pred = self.predicate("all") if predicate is None else predicate
        rx_records = []
        fft_records = []

        def collect(cluster):
            rx_records.append(cluster.get_rx_gain())
            fft_records.append(cluster.get_fft_gain())

        # Enable AGC (unlock gains) and record what it converges to
        self.pool.set_gain_settings({"rx_gain_enable": False, "fft_scale_enable": False})
        handle = self.pool.add_csi_callback(collect, cb_predicate=pred)
        self._drive_for(duration, drive)
        self.pool.remove_csi_callback(handle)

        if not rx_records or not fft_records:
            if fallback is not None:
                self.pool.set_gain_settings(dict(fallback))
                return None
            raise RuntimeError("no radar packets collected during gain calibration")

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Mean of empty slice", category=RuntimeWarning)
            rx_gain = np.rint(np.nanmean(np.asarray(rx_records, dtype=np.float32), axis=0))
            fft_gain = np.rint(np.nanmean(np.asarray(fft_records, dtype=np.float32), axis=0))
        # Freeze a sensor only if BOTH its RX and FFT gains are known; otherwise keep it on AGC.
        # Tie the two enable masks together so rx_gain_enable == fft_scale_enable for every sensor
        # (the hardware / pool drawer require the two to be consistent — e.g. a sensor on a different
        # array than the transmitter may have a known RX gain but no FFT gain, or vice versa).
        finite = np.isfinite(rx_gain) & np.isfinite(fft_gain)
        gains = {
            "rx_gain_enable": finite,
            "rx_gain_value": np.nan_to_num(rx_gain, nan=0.0).astype(int),
            "fft_scale_enable": finite,
            "fft_scale_value": np.nan_to_num(fft_gain, nan=0.0).astype(int),
        }
        self.pool.set_gain_settings(gains)

        # Let the locked gains take effect, discarding transitional packets
        self._drive_for(settle, drive)
        return gains

    def _drive_for(self, duration, drive):
        if not duration or duration <= 0:
            return
        if drive:
            end = time.monotonic() + duration
            while time.monotonic() < end:
                self.pool.run()
        else:
            time.sleep(duration)

    def stop(self):
        """Disable radar transmission on all antennas."""
        self.pool.set_radar_config({"active_by_antid": [False] * constants.ANTENNAS_PER_BOARD})
