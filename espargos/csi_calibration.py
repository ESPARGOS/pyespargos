#!/usr/bin/env python

"""CSI-specific view of the per-sensor phase and time calibration.

The generic calibration model lives in :mod:`espargos.sensor_calibration`:
every sensor is described by one timing offset and one phase offset.
:class:`CSICalibration` combines that model with the Wi-Fi channel
configuration to synthesize and cache per-format calibration vectors (L-LTF,
HT20, HT40, HE20) on their respective subcarrier grids. Because the stored
model is format-independent, calibration measured with one preamble format
applies to every other format on the same channel configuration, e.g.
calibrating from L-LTF packets in 40 MHz mode and coherently receiving HT40.
"""

import numpy as np
import logging

from . import board
from . import constants
from . import csi_processing
from .sensor_calibration import ClockReferenceScope, SensorCalibration, compute_reference_path_delays


class CSICalibration(SensorCalibration):
    def __init__(
        self,
        boards: list[board.Board],
        channel_primary: int,
        channel_secondary_relative: int,
        timing_offsets: np.ndarray,
        phase_offsets: np.ndarray,
        board_cable_lengths=None,
        board_cable_vfs=None,
        clock_scope: ClockReferenceScope | str = ClockReferenceScope.POOL,
    ):
        """
        Constructor for the CSICalibration class.

        This class stores the per-sensor phase and time calibration for a Wi-Fi channel
        configuration and applies it to CSI data of any preamble format. It also supports
        multi-board setups with different lengths for the cables that distribute the clock
        and phase calibration signal.

        Timing and phase offsets describe the raw measured offsets of the calibration path,
        e.g. from :func:`espargos.csi_processing.estimate_phase_time_offsets`. Delays that
        exist only on the calibration path (reference distribution traces on the PCB and,
        if provided, the reference distribution cables between boards) are removed here, so
        that they are not "corrected" on over-the-air data.

        :param boards: A list of :class:`.board.Board` objects that make up the pool
        :param channel_primary: The primary channel number
        :param channel_secondary_relative: Relative position of the secondary channel:
            ``-1`` for HT40 below, ``+1`` for HT40 above, and ``0`` for a plain 20 MHz channel
        :param timing_offsets: Measured per-sensor timing offsets in seconds, relative to the
            reference sensor, as a real-valued array of shape
            :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)`
        :param phase_offsets: Measured per-sensor phase offsets, relative to the reference
            sensor, with the same shape. May be provided as complex values on the unit circle
            or as angles in radians.
        :param board_cable_lengths: The lengths of the cables that distribute the clock and
            phase calibration signal to the boards, in meters
        :param board_cable_vfs: The velocity factors of those cables
        :param clock_scope: Whether :code:`timing_offsets` use one pool-wide reference
            or one independent reference per board.
        """
        self.boards = boards
        self.channel_primary = channel_primary
        self.channel_secondary_relative = channel_secondary_relative

        sensor_shape = (
            len(boards),
            constants.ROWS_PER_BOARD,
            constants.ANTENNAS_PER_ROW,
        )

        timing_offsets = np.asarray(timing_offsets, dtype=np.float64)
        if np.iscomplexobj(phase_offsets):
            phase_offsets = np.angle(phase_offsets)
        phase_offsets = np.asarray(phase_offsets, dtype=np.float64)

        reference_path_delays = compute_reference_path_delays(boards, board_cable_lengths, board_cable_vfs)

        # The estimated offsets are referenced to the receiver LO center (the HT40
        # mid-frequency in 40 MHz mode): CSICluster.deserialize_csi_* applies its
        # timestamp-based STO corrections on LO-referenced subcarrier grids, so the
        # measured offsets, which absorb the per-sensor timestamp epochs, are anchored
        # there. (HE20 deserialization uses a primary-channel-centered grid, but HE20
        # cannot be received with channel bonding, so its reference coincides.)
        channel_secondary = channel_primary + 4 * channel_secondary_relative
        phase_reference_frequency = csi_processing.get_center_frequency(channel_primary, channel_secondary)

        super().__init__(
            sensor_shape=sensor_shape,
            timing_offsets=timing_offsets,
            phase_offsets=phase_offsets,
            phase_reference_frequency=phase_reference_frequency,
            clock_scope=clock_scope,
            reference_path_delays=reference_path_delays,
        )

        self.logger = logging.getLogger("espargos.calib")
        self._calibration_cache_by_format: dict[str, np.ndarray] = {}

    def _format_frequencies(self, csi_format: str) -> np.ndarray:
        if csi_format == "lltf":
            return csi_processing.get_frequencies_lltf(self.channel_primary)
        if csi_format == "ht20":
            return csi_processing.get_frequencies_ht20(self.channel_primary)
        if csi_format == "he20":
            return csi_processing.get_frequencies_he20(self.channel_primary)
        if csi_format == "ht40":
            channel_secondary = self.channel_primary + 4 * self.channel_secondary_relative
            return csi_processing.get_frequencies_ht40(self.channel_primary, channel_secondary)
        raise ValueError(f"Unknown CSI format {csi_format!r}")

    def _apply(self, csi_format: str, values: np.ndarray) -> np.ndarray:
        correction = self._calibration_cache_by_format.get(csi_format)
        if correction is None:
            if np.isnan(self.timing_offsets).any() or np.isnan(self.phase_offsets).any():
                self.logger.warning("Calibration offsets contain NaN, missing calibration data?")

            correction = self.phase_time_correction(self._format_frequencies(csi_format))
            self._calibration_cache_by_format[csi_format] = correction

        return values * correction

    def apply_lltf(self, values: np.ndarray) -> np.ndarray:
        """
        Apply phase calibration to the provided L-LTF CSI data.

        :param values: The CSI data to which the phase calibration should be applied, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL)`
        :return: The phase-calibrated CSI data
        """
        return self._apply("lltf", values)

    def apply_ht20(self, values: np.ndarray) -> np.ndarray:
        """
        Apply phase calibration to the provided HT20 CSI data.

        :param values: The CSI data to which the phase calibration should be applied, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi_packet.HT_COEFFICIENTS_PER_CHANNEL)`
        :return: The phase-calibrated CSI data
        """
        return self._apply("ht20", values)

    def apply_ht40(self, values: np.ndarray) -> np.ndarray:
        """
        Apply phase calibration to the provided HT40 CSI data.

        :param values: The CSI data to which the phase calibration should be applied, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi_packet.HT_COEFFICIENTS_PER_CHANNEL + csi_packet.HT40_GAP_SUBCARRIERS + csi_packet.HT_COEFFICIENTS_PER_CHANNEL)`
        :return: The phase-calibrated CSI data
        """
        return self._apply("ht40", values)

    def apply_he20(self, values: np.ndarray) -> np.ndarray:
        """
        Apply phase calibration to the provided HE20 CSI data.

        :param values: The CSI data to which the phase calibration should be
            applied, as a complex-valued numpy array of shape
            :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi_packet.HE20_COEFFICIENTS_PER_CHANNEL)`
        :return: The phase-calibrated CSI data
        """
        return self._apply("he20", values)
