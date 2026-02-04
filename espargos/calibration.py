#!/usr/bin/env python3

import numpy as np
import logging

from . import constants
from . import board
from . import util
from . import csi


class CSICalibration(object):
    def __init__(
        self,
        boards: list[board.Board],
        channel_primary: int,
        channel_secondary: int,
        calibration_values_lltf: np.ndarray,
        calibration_values_ht20: np.ndarray,
        calibration_values_ht40: np.ndarray,
        board_cable_lengths=None,
        board_cable_vfs=None,
    ):
        """
        Constructor for the CSICalibration class.

        This class takes care of storing and applying the phase calibration values for the CSI data as well as calibrating phases.
        It also supports multi-board setups with different lengths for the cables that distribute the clock and phase calibration signal.
        Note: Single-channel calibration is currently not yet supported, must always calibrate whole 40MHz channel.

        :param revisions: A list of :class:`.revisions.BoardRevision` objects that specify the board revisions of the ESPARGOS boards in the pool
        :param channel_primary: The primary channel number
        :param channel_secondary: The secondary channel number. Must be equal to :code:`channel_primary + 4` or :code:`channel_primary - 4` if channel bonding is used, otherwise must be equal to :code:`channel_primary`
        :param calibration_values_lltf: The phase calibration values for the L-LTF channel, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi.LEGACY_COEFFICIENTS_PER_CHANNEL)`
        :param calibration_values_ht20: The phase calibration values for the HT20 channel, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi.HT_COEFFICIENTS_PER_CHANNEL)`
        :param calibration_values_ht40: The phase calibration values for the HT40 channel, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS + csi.HT_COEFFICIENTS_PER_CHANNEL)`
        :param board_cable_lengths: The lengths of the cables that distribute the clock and phase calibration signal to the ESP32 boards, in meters
        :param board_cable_vfs: The velocity factors of the cables that distribute the clock and phase calibration signal to the ESP32 boards
        """
        assert calibration_values_lltf.shape == (
            len(boards),
            constants.ROWS_PER_BOARD,
            constants.ANTENNAS_PER_ROW,
            csi.LEGACY_COEFFICIENTS_PER_CHANNEL,
        )
        assert calibration_values_ht20.shape == (
            len(boards),
            constants.ROWS_PER_BOARD,
            constants.ANTENNAS_PER_ROW,
            csi.HT_COEFFICIENTS_PER_CHANNEL,
        )
        assert calibration_values_ht40.shape == (
            len(boards),
            constants.ROWS_PER_BOARD,
            constants.ANTENNAS_PER_ROW,
            csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS + csi.HT_COEFFICIENTS_PER_CHANNEL,
        )

        self.logger = logging.getLogger("espargos.calib")

        self.channel_primary = channel_primary
        self.channel_secondary = channel_secondary
        # wavelengths_lltf = util.get_calib_trace_wavelength(self.frequencies_lltf).astype(calibration_values_lltf.dtype)
        # wavelengths_ht40 = util.get_calib_trace_wavelength(self.frequencies_ht40).astype(calibration_values_ht40.dtype)
        # tracelengths = np.asarray(constants.CALIB_TRACE_LENGTH, dtype = calibration_values_ht40.dtype)# - np.asarray(constants.CALIB_TRACE_EMPIRICAL_ERROR)

        # If provided, determine delay due to different sync signal distribution cable lengths and velocity factors for each board
        cable_group_delays = np.zeros(len(boards), dtype=calibration_values_ht40.dtype)
        if board_cable_lengths is not None:
            assert board_cable_vfs is not None
            assert len(board_cable_lengths) == len(boards)
            assert len(board_cable_vfs) == len(boards)
            board_cable_lengths = np.asarray(board_cable_lengths, dtype=calibration_values_ht40.dtype)
            board_cable_vfs = np.asarray(board_cable_vfs, dtype=calibration_values_ht40.dtype)
            cable_group_delays[:] = board_cable_lengths / (constants.SPEED_OF_LIGHT * board_cable_vfs)

        # Determine per-antenna total group delay based on cable lengths, velocity factors and board revisions
        group_delays = np.zeros(calibration_values_ht40.shape[:-1], dtype=calibration_values_ht40.dtype)
        for b, board in enumerate(boards):
            group_delays[b, :, :] = cable_group_delays[b] + board.revision.calib_trace_delays

        # From group delay (in seconds) to phase shift per subcarrier
        prop_phase_offsets_lltf = np.exp(-1.0j * 2 * np.pi * group_delays[:, :, :, np.newaxis] * util.get_frequencies_lltf(self.channel_primary)[np.newaxis, np.newaxis, np.newaxis, :])
        prop_phase_offsets_ht20 = np.exp(-1.0j * 2 * np.pi * group_delays[:, :, :, np.newaxis] * util.get_frequencies_ht20(self.channel_primary)[np.newaxis, np.newaxis, np.newaxis, :])
        prop_phase_offsets_ht40 = np.exp(-1.0j * 2 * np.pi * group_delays[:, :, :, np.newaxis] * util.get_frequencies_ht40(self.channel_primary, self.channel_secondary)[np.newaxis, np.newaxis, np.newaxis, :])

        # prop_calib_each_board_lltf = np.exp(-1.0j * 2 * np.pi * tracelengths[:,:,np.newaxis] / wavelengths_lltf[np.newaxis, np.newaxis])
        # prop_calib_each_board_ht40 = np.exp(-1.0j * 2 * np.pi * tracelengths[:,:,np.newaxis] / wavelengths_ht40[np.newaxis, np.newaxis])
        # prop_delay_each_board = np.asarray(constants.CALIB_TRACE_LENGTH) / np.asarray(constants.CALIB_TRACE_GROUP_VELOCITY)
        self.receiver_lo_freq = constants.WIFI_CHANNEL1_FREQUENCY + constants.WIFI_CHANNEL_SPACING * ((self.channel_primary + self.channel_secondary) / 2 - 1)

        self.calibration_values_lltf = np.einsum("bras,bras->bras", calibration_values_lltf, np.conj(prop_phase_offsets_lltf))
        self.calibration_values_ht20 = np.einsum("bras,bras->bras", calibration_values_ht20, np.conj(prop_phase_offsets_ht20))
        self.calibration_values_ht40 = np.einsum("bras,bras->bras", calibration_values_ht40, np.conj(prop_phase_offsets_ht40))

        ## Account for additional board-specific phase offsets due to different feeder cable lengths in a multi-board antenna array system
        # if board_cable_lengths is not None:
        #    assert(board_cable_vfs is not None)
        #    board_cable_lengths = np.asarray(board_cable_lengths)
        #    board_cable_vfs = np.asarray(board_cable_vfs)

        #    subcarrier_cable_wavelengths_lltf = util.get_cable_wavelength(util.get_frequencies_lltf(channel_primary), board_cable_vfs).astype(calibration_values_lltf.dtype)
        #    subcarrier_cable_wavelengths_ht40 = util.get_cable_wavelength(util.get_frequencies_ht40(channel_primary, channel_secondary), board_cable_vfs).astype(calibration_values_ht40.dtype)

        #    board_phase_offsets_lltf = np.exp(-1.0j * 2 * np.pi * board_cable_lengths[:,np.newaxis] / subcarrier_cable_wavelengths_lltf)
        #    board_phase_offsets_ht40 = np.exp(-1.0j * 2 * np.pi * board_cable_lengths[:,np.newaxis] / subcarrier_cable_wavelengths_ht40)

        #    prop_calib_lltf = np.einsum("bs,ras->bras", board_phase_offsets_lltf, prop_calib_each_board_lltf)
        #    prop_calib_ht40 = np.einsum("bs,ras->bras", board_phase_offsets_ht40, prop_calib_each_board_ht40)

        #    coeffs_without_propdelay_lltf = np.einsum("bras,bras->bras", calibration_values_lltf, np.conj(prop_calib_lltf))
        #    coeffs_without_propdelay_ht40 = np.einsum("bras,bras->bras", calibration_values_ht40, np.conj(prop_calib_ht40))
        # else:
        #    coeffs_without_propdelay_lltf = np.einsum("bras,ras->bras", calibration_values_lltf, np.conj(prop_calib_each_board_lltf))
        #    coeffs_without_propdelay_ht40 = np.einsum("bras,ras->bras", calibration_values_ht40, np.conj(prop_calib_each_board_ht40))

        # self.calibration_values_lltf: np.ndarray = np.exp(-1.0j * np.angle(coeffs_without_propdelay_lltf))
        # self.calibration_values_ht40: np.ndarray = np.exp(-1.0j * np.angle(coeffs_without_propdelay_ht40))

        # self.timestamp_calibration_values = timestamp_calibration_values - prop_delay_each_board[np.newaxis,:,:]

    def apply_ht40(self, values: np.ndarray) -> np.ndarray:
        """
        Apply phase calibration to the provided HT40 CSI data.
        Also accounts for subcarrier-specific phase offsets, e.g., due to low-pass filter characteristic of baseband signal path inside the ESP32.

        :param values: The CSI data to which the phase calibration should be applied, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS + csi.HT_COEFFICIENTS_PER_CHANNEL)`
        :return: The phase-calibrated CSI data
        """
        # TODO: Check if primary and secondary channel match
        # Check if calibration values are not NaN
        if np.isnan(self.calibration_values_ht40).any():
            self.logger.warning("HT40 calibration values contain NaN, missing calibration data?")

        # Only calibrate phase, not amplitude
        return values * np.exp(-1.0j * np.angle(self.calibration_values_ht40))

    def apply_ht20(self, values: np.ndarray) -> np.ndarray:
        """
        Apply phase calibration to the provided HT20 CSI data.
        Also accounts for subcarrier-specific phase offsets, e.g., due to low-pass filter characteristic of baseband signal path inside the ESP32.

        :param values: The CSI data to which the phase calibration should be applied, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi.HT_COEFFICIENTS_PER_CHANNEL)`
        :return: The phase-calibrated CSI data
        """
        # TODO: Check if calibration value channel matches OTA channel

        # Check if calibration values are not NaN
        if np.isnan(self.calibration_values_ht20).any():
            self.logger.warning("HT20 calibration values contain NaN, missing calibration data?")

        return values * np.exp(-1.0j * np.angle(self.calibration_values_ht20))

    def apply_lltf(self, values: np.ndarray) -> np.ndarray:
        """
        Apply phase calibration to the provided L-LTF CSI data.
        Also accounts for subcarrier-specific phase offsets, e.g., due to low-pass filter characteristic of baseband signal path inside the ESP32.

        :param values: The CSI data to which the phase calibration should be applied, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS + csi.HT_COEFFICIENTS_PER_CHANNEL)`
        :return: The phase-calibrated CSI data
        """
        # TODO: Check if calibration value channel matches OTA channel

        # Check if calibration values are not NaN
        if np.isnan(self.calibration_values_lltf).any():
            self.logger.warning("L-LTF calibration values contain NaN, missing calibration data?")

        return values * np.exp(-1.0j * np.angle(self.calibration_values_lltf))

    def apply_timestamps(self, timestamps: np.ndarray):
        """
        Apply time offset calibration to the provided timestamps.

        :param timestamps: The timestamps to which the calibration should be applied, as a numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)`
        :return: The calibrated timestamps
        """
        # TODO
        return timestamps  # - self.timestamp_calibration_values
