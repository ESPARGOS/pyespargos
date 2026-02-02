#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import DemoApplication

import numpy as np
import espargos
import argparse

import PyQt6.QtCharts
import PyQt6.QtCore

class EspargosDemoInstantaneousCSI(DemoApplication):
	def __init__(self, argv):
		# Parse command line arguments
		parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Show instantaneous CSI over subcarrier index (single board)", add_help = False)
		parser.add_argument("hosts", type = str, help = "Comma-separated list of host addresses (IP or hostname) of ESPARGOS controllers")
		parser.add_argument("-b", "--backlog", type = int, default = 20, help = "Number of CSI datapoints to average over in backlog")
		parser.add_argument("-s", "--shift-peak", default = False, help = "Time-shift CSI so that first peaks align", action = "store_true")
		parser.add_argument("-o", "--oversampling", type = int, default = 4, help = "Oversampling factor for time-domain CSI")
		parser.add_argument("--no-calib", default = False, help = "Do not calibrate", action = "store_true")
		display_group = parser.add_mutually_exclusive_group()
		display_group.add_argument("-t", "--timedomain", default = False, help = "Display CSI in time-domain", action = "store_true")
		display_group.add_argument("-m", "--music", default = False, help = "Display PDP computed via MUSIC algorithm", action = "store_true")
		display_group.add_argument("-v", "--mvdr", default = False, help = "Display PDP computed via MVDR algorithm", action = "store_true")
		format_group = parser.add_mutually_exclusive_group()
		format_group.add_argument("-l", "--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
		format_group.add_argument("-ht40", "--ht40", default = False, help = "Use only CSI from HT40", action = "store_true")
		format_group.add_argument("-ht20", "--ht20", default = False, help = "Use only CSI from HT20", action = "store_true")
		super().__init__(argv, argparse_parent = parser)

		# Check if at least one format is selected
		if not (self.args.lltf or self.args.ht40 or self.args.ht20):
			print("Error: At least one of --lltf, --ht40 or --ht20 must be selected.")
			sys.exit(1)

		# Set up ESPARGOS pool and backlog
		hosts = self.args.hosts.split(",")
		self.initialize_pool(hosts, enable_backlog = True, calibrate = not self.args.no_calib)

		# Value range handling
		self.stable_power_minimum = None
		self.stable_power_maximum = None

		if self.args.lltf:
			self.subcarrier_count = espargos.csi.LEGACY_COEFFICIENTS_PER_CHANNEL
		elif self.args.ht40:
			self.subcarrier_count = 2 * espargos.csi.HT_COEFFICIENTS_PER_CHANNEL + espargos.csi.HT40_GAP_SUBCARRIERS
		else:
			self.subcarrier_count = espargos.csi.HT_COEFFICIENTS_PER_CHANNEL

		self.sensor_count = len(hosts) * espargos.constants.ANTENNAS_PER_BOARD
		self.subcarrier_range = np.arange(-self.subcarrier_count // 2, self.subcarrier_count // 2)

		self.init_qml(pathlib.Path(__file__).resolve().parent / "instantaneous-csi-ui.qml")

	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def sensorCount(self):
		return np.prod(self.pool.get_shape())

	@PyQt6.QtCore.pyqtProperty(list, constant=True)
	def subcarrierRange(self):
		return self.subcarrier_range.tolist()

	def exec(self):
		return super().exec()

	def _interpolate_axis_range(self, previous, new):
		if previous is None:
			return new
		else:
			return (previous * 0.97 + new * 0.03)

	# list parameters contain PyQt6.QtCharts.QLineSeries
	@PyQt6.QtCore.pyqtSlot(list, list, PyQt6.QtCharts.QValueAxis)
	def updateCSI(self, powerSeries, phaseSeries, axis):
		if not hasattr(self, "backlog"):
			# Backlog not yet initialized
			return

		if self.args.lltf:
			csi_key = "lltf"
		elif self.args.ht40:
			csi_key = "ht40"
		else:
			csi_key = "ht20"
		csi_backlog, rssi_backlog = self.backlog.get_multiple([csi_key, "rssi"])

		# If any value is NaN, skip this update (happens if received frame were not of expected type)
		if np.isnan(csi_backlog).any() or np.isnan(rssi_backlog).any():
			return

		# Weight CSI data with RSSI
		csi_backlog = csi_backlog * 10**(rssi_backlog[..., np.newaxis] / 20)

		if self.args.shift_peak:
			espargos.util.remove_mean_sto(csi_backlog)

		# Fill "gap" in subcarriers with interpolated data
		if self.args.ht40:
			espargos.util.interpolate_ht40ltf_gap(csi_backlog)
		elif self.args.ht20:
			espargos.util.interpolate_ht20ltf_gap(csi_backlog)
		elif self.args.lltf:
			pass
			#espargos.util.interpolate_lltf_gap(csi_backlog)

		# TODO: If using per-board calibration, interpolation should also be per-board
		csi_interp = espargos.util.csi_interp_iterative(csi_backlog, iterations = 5)
		csi_flat = np.reshape(csi_interp, (-1, csi_interp.shape[-1]))

		if self.args.mvdr or self.args.music:
			if self.args.music:
				superres_delays, superres_pdps = espargos.util.fdomain_to_tdomain_pdp_music(csi_backlog)
			else:
				superres_delays, superres_pdps = espargos.util.fdomain_to_tdomain_pdp_mvdr(csi_backlog)

			superres_pdps_flat = np.reshape(superres_pdps, (-1, superres_pdps.shape[-1]))

			superres_pdps_flat = superres_pdps_flat / np.max(superres_pdps_flat)
			self.stable_power_minimum = 0
			self.stable_power_maximum = 1.1

			for pwr_series, mvdr_pdp in zip(powerSeries, superres_pdps_flat):
				pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(superres_delays, mvdr_pdp)])
		elif self.args.timedomain:
			csi_flat_zeropadded = np.zeros((csi_flat.shape[0], csi_flat.shape[1] * self.args.oversampling), dtype = np.complex64)
			subcarriers = csi_flat.shape[1]
			subcarriers_zp = csi_flat_zeropadded.shape[1]
			csi_flat_zeropadded[:,subcarriers_zp // 2 - subcarriers // 2:subcarriers_zp // 2 + subcarriers // 2 + 1] = csi_flat
			csi_flat_zeropadded = np.fft.ifftshift(np.fft.ifft(np.fft.fftshift(csi_flat_zeropadded, axes = -1), axis = -1), axes = -1)
			subcarrier_range_zeropadded = np.arange(-csi_flat_zeropadded.shape[-1] // 2, csi_flat_zeropadded.shape[-1] // 2) / self.args.oversampling
			csi_power = (csi_flat_zeropadded.shape[1] * np.abs(csi_flat_zeropadded))**2
			self.stable_power_minimum = 0
			self.stable_power_maximum = self._interpolate_axis_range(self.stable_power_maximum, np.max(csi_power) * 1.1)

			axis.setMin(0)
			axis.setMax(csi_flat_zeropadded.shape[-1] / np.sqrt(2) / self.args.oversampling**2)
			csi_phase = np.angle(csi_flat_zeropadded * np.exp(-1.0j * np.angle(csi_flat_zeropadded[0, len(csi_flat_zeropadded[0]) // 2])))

			for pwr_series, phase_series, ant_pwr, ant_phase in zip(powerSeries, phaseSeries, csi_power, csi_phase):
				pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range_zeropadded, ant_pwr)])
				phase_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range_zeropadded, ant_phase)])
		else:
			csi_power = 20 * np.log10(np.abs(csi_flat) + 0.00001)
			self.stable_power_minimum = self._interpolate_axis_range(self.stable_power_minimum, np.min(csi_power) - 3)
			self.stable_power_maximum = self._interpolate_axis_range(self.stable_power_maximum, np.max(csi_power) + 3)
			csi_phase = np.angle(csi_flat * np.exp(-1.0j * np.angle(csi_flat[0, csi_flat.shape[1] // 2])))
			#csi_phase = np.angle(csi_flat * np.exp(-1.0j * np.angle(csi_flat[0, :])))

			for pwr_series, phase_series, ant_pwr, ant_phase in zip(powerSeries, phaseSeries, csi_power, csi_phase):
				pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(self.subcarrier_range, ant_pwr)])
				phase_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(self.subcarrier_range, ant_phase)])

		axis.setMin(self.stable_power_minimum)
		axis.setMax(self.stable_power_maximum)

	@PyQt6.QtCore.pyqtProperty(bool, constant=True)
	def timeDomain(self):
		return self.args.timedomain

	@PyQt6.QtCore.pyqtProperty(bool, constant=True)
	def superResolution(self):
		return self.args.mvdr or self.args.music


app = EspargosDemoInstantaneousCSI(sys.argv)
sys.exit(app.exec())
