#!/usr/bin/env python

import numpy as np
from abc import ABC, abstractmethod

from . import constants

# Helper module defining board revision-specific constants
__all__ = ["BoardRevision", "BoardRevisionDensiflorus"]


class BoardRevision(ABC):
    @property
    @abstractmethod
    def identification(self) -> tuple:
        """Return the controller identification tuple for this revision."""

    @property
    def calib_trace_delays(self) -> list:
        """Calibration trace signal delays on ESPARGOS PCB [in s]"""
        effective_dielectric_constant = (self._calib_trace_dielectric_constant + 1) / 2 + (self._calib_trace_dielectric_constant - 1) / 2 * (1 + 12 * (self._calib_trace_height / self._calib_trace_width)) ** (-1 / 2)
        group_velocity = constants.SPEED_OF_LIGHT / effective_dielectric_constant**0.5
        return self._calib_trace_lengths / group_velocity

    @abstractmethod
    def esp_num_to_row_col(self, esp_num: int) -> tuple:
        """Convert ESP number to (row, column) on the board"""

    def antenna_id_to_row_col(self, antenna_id: int) -> tuple:
        """Convert firmware antenna id to (row, column) on this board revision."""
        return self.esp_num_to_row_col(self._antenna_id_to_esp_num[antenna_id])

    def sensor_values_to_antenna_id_list(self, values, name: str = "values") -> list:
        """Convert a board-local (row, column) array to firmware antenna-id order."""
        array = np.asarray(values)
        expected_shape = (constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)
        if array.shape != expected_shape:
            raise ValueError(f"{name} must use (row, column) shape {expected_shape}, got {array.shape}")

        values_by_antenna_id = []
        for antenna_id in range(constants.ANTENNAS_PER_BOARD):
            row, col = self.antenna_id_to_row_col(antenna_id)
            value = array[row, col]
            values_by_antenna_id.append(value.item() if hasattr(value, "item") else value)
        return values_by_antenna_id

    @property
    @abstractmethod
    def _antenna_id_to_esp_num(self) -> dict:
        """Map firmware antenna IDs to ESP indices."""

    # Private, (potentially) revision-specific properties
    @property
    @abstractmethod
    def _calib_trace_dielectric_constant(self) -> float:
        """Dielectric constant of the PCB material"""

    @property
    @abstractmethod
    def _calib_trace_lengths(self) -> list:
        """Lengths of the calibration traces on the PCB [in m]"""

    @property
    @abstractmethod
    def _calib_trace_width(self) -> float:
        """Width of the calibration trace [in mm]"""

    @property
    def _calib_trace_height(self) -> float:
        """Height of the calibration trace (distance between GND plane and microstrip) [in mm]"""
        return 0.119


# Codename "ESPARGOS-DENSIFLORUS" (2025/2026 PCB)
class BoardRevisionDensiflorus(BoardRevision):
    @property
    def identification(self) -> tuple:
        return ("espargos", "densiflorus")

    def esp_num_to_row_col(self, esp_num: int) -> tuple:
        row = 1 - esp_num // 4
        col = 3 - esp_num % 4
        return (row, col)

    @property
    def _antenna_id_to_esp_num(self) -> dict:
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


_ALL_REVISIONS = (BoardRevisionDensiflorus(),)
