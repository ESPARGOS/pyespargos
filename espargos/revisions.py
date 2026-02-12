#!/usr/bin/env python

import numpy as np

from . import constants
from . import csi


# Helper module defining board revision-specific constants
class BoardRevision:
    @property
    def identification(self) -> tuple:
        raise NotImplementedError

    @property
    def type_header(self) -> int:
        raise NotImplementedError

    @property
    def csistream_pkt_t(self) -> type:
        raise NotImplementedError

    @property
    def serialized_csi_t(self) -> type:
        raise NotImplementedError

    @property
    def calib_trace_delays(self) -> list:
        """Calibration trace signal delays on ESPARGOS PCB [in s]"""
        effective_dielectric_constant = (self._calib_trace_dielectric_constant + 1) / 2 + (self._calib_trace_dielectric_constant - 1) / 2 * (1 + 12 * (self._calib_trace_height / self._calib_trace_width)) ** (-1 / 2)
        group_velocity = constants.SPEED_OF_LIGHT / effective_dielectric_constant**0.5
        return self._calib_trace_lengths / group_velocity

    def esp_num_to_row_col(self, esp_num: int) -> tuple:
        """Convert ESP number to (row, column) on the board"""
        raise NotImplementedError

    # Private, (potentially) revision-specific properties
    @property
    def _calib_trace_dielectric_constant(self) -> float:
        """Dielectric constant of the PCB material"""
        return NotImplementedError

    @property
    def _calib_trace_lengths(self) -> list:
        """Lengths of the calibration traces on the PCB [in m]"""
        return NotImplementedError

    @property
    def _calib_trace_width(self) -> float:
        """Width of the calibration trace [in mm]"""
        return NotImplementedError

    @property
    def _calib_trace_height(self) -> float:
        """Height of the calibration trace (distance between GND plane and microstrip) [in mm]"""
        return 0.119


# Codename "ESPARGOS-DENSIFLORUS" (2025/2026 PCB)
class BoardRevisionDensiflorus(BoardRevision):
    @property
    def identification(self) -> tuple:
        return ("espargos", "densiflorus")

    @property
    def type_header(self) -> int:
        return 0xE4CD0BAC

    @property
    def csistream_pkt_t(self) -> type:
        return csi.csistream_pkt_v3_t

    @property
    def serialized_csi_t(self) -> type:
        return csi.serialized_csi_v3_t

    def esp_num_to_row_col(self, esp_num: int) -> tuple:
        row = 1 - esp_num // 4
        col = 3 - esp_num % 4
        return (row, col)

    @property
    def antid_to_esp_num(self) -> dict:
        return {
            0: 3,  # Sensor 0 -> ESP 0
            1: 2,  # Sensor 1 -> ESP 1
            2: 1,  # Sensor 2 -> ESP 2
            3: 0,  # Sensor 3 -> ESP 3
            4: 7,  # Sensor 4 -> ESP 4
            5: 6,  # Sensor 5 -> ESP 5
            6: 5,  # Sensor 6 -> ESP 6
            7: 4,  # Sensor 7 -> ESP 7
        }

    # Private, (potentially) revision-specific properties
    @property
    def _calib_trace_dielectric_constant(self) -> float:
        return 4.3

    @property
    def _calib_trace_lengths(self) -> list:
        return np.asarray(
            [
                [0.0604561, 0.0373554, 0.1070395, 0.1770280],
                [0.1076842, 0.0554654, 0.0806678, 0.1462569],
            ]
        )

    @property
    def _calib_trace_width(self) -> float:
        return 0.2

    @property
    def _calib_trace_height(self) -> float:
        return 0.119


all_revisions = [BoardRevisionDensiflorus()]
