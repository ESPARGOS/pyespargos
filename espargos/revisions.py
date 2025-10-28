#!/usr/bin/env python3

import numpy as np

from . import constants
from . import csi

# Helper module defining board revision-specific constants
class BoardRevision:
	@property
	def identification(self) -> str:
		raise NotImplementedError

	@property
	def type_header(self) -> int:
		raise NotImplementedError
	
	@property
	def serialized_csi_t(self) -> type:
		raise NotImplementedError
	
	@property
	def calib_trace_delays(self) -> list:
		"""Calibration trace signal delays on ESPARGOS PCB [in s]"""
		effective_dielectric_constant = (self._calib_trace_dielectric_constant + 1) / 2 + (self._calib_trace_dielectric_constant - 1) / 2 * (1 + 12 * (self._calib_trace_height/self._calib_trace_width))**(-1/2)
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


# Original ESPARGOS Prototype (2024 PCB)
class BoardRevisionPrototype(BoardRevision):
	@property
	def identification(self) -> str:
		return "ESPARGOS"

	@property
	def type_header(self) -> int:
		return 0x5a1f19b1

	@property
	def serialized_csi_t(self) -> type:
		return csi.serialized_csi_v1_t

	def esp_num_to_row_col(self, esp_num: int) -> tuple:
		row = 1 - esp_num // 4
		col = 3 - esp_num % 4
		return (row, col)

	# Private, (potentially) revision-specific properties
	@property
	def _calib_trace_dielectric_constant(self) -> float:
		return 4.3

	@property
	def _calib_trace_lengths(self) -> list:
		return np.asarray([[0.0708462, 0.0229349, 0.0786856, 0.1423600], [0.0838888, 0.0295291, 0.0671322, 0.1308537]])
	
	@property
	def _calib_trace_width(self) -> float:
		return 0.2
	
	@property
	def _calib_trace_height(self) -> float:
		return 0.119

# Codename "ESPARGOS-DENSIFLORUS" (2025/2026 PCB)
class BoardRevisionDensiflorus(BoardRevision):
	@property
	def identification(self) -> str:
		return "ESPARGOS-DENSIFLORUS"

	@property
	def type_header(self) -> int:
		return 0xe4cd0bac

	@property
	def serialized_csi_t(self) -> type:
		return csi.serialized_csi_v3_t

	def esp_num_to_row_col(self, esp_num: int) -> tuple:
		row = 1 - esp_num // 4
		col = 3 - esp_num % 4
		return (row, col)

	# Private, (potentially) revision-specific properties
	@property
	def _calib_trace_dielectric_constant(self) -> float:
		return 4.3

	@property
	def _calib_trace_lengths(self) -> list:
		return np.asarray([[0.0604561, 0.0373554, 0.1070395, 0.1770280], [0.1076842, 0.0554654, 0.0806678, 0.1462569]])
	
	@property
	def _calib_trace_width(self) -> float:
		return 0.2
	
	@property
	def _calib_trace_height(self) -> float:
		return 0.119
	
all_revisions = [BoardRevisionPrototype(), BoardRevisionDensiflorus()]