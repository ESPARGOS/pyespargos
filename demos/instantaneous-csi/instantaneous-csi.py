#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import numpy as np
import espargos
import argparse

import PyQt6.QtWidgets
import PyQt6.QtCharts
import PyQt6.QtCore
import PyQt6.QtQml

class EspargosDemoInstantaneousCSI(PyQt6.QtWidgets.QApplication):
	def __init__(self, argv):
		super().__init__(argv)

		# Parse command line arguments
		parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Show instantaneous CSI over subcarrier index (single board)")
		parser.add_argument("hosts", type = str, help = "Comma-separated list of host addresses (IP or hostname) of ESPARGOS controllers")
		parser.add_argument("-b", "--backlog", type = int, default = 20, help = "Number of CSI datapoints to average over in backlog")
		parser.add_argument("-s", "--shift-peak", default = False, help = "Time-shift CSI so that first peaks align", action = "store_true")
		parser.add_argument("--l20", default = False, help = "Operate on 20MHz band", action = "store_true")
		parser.add_argument("-o", "--oversampling", type = int, default = 4, help = "Oversampling factor for time-domain CSI")
		display_group = parser.add_mutually_exclusive_group()
		display_group.add_argument("-t", "--timedomain", default = False, help = "Display CSI in time-domain", action = "store_true")
		display_group.add_argument("-m", "--music", default = False, help = "Display PDP computed via MUSIC algorithm", action = "store_true")
		display_group.add_argument("-v", "--mvdr", default = False, help = "Display PDP computed via MVDR algorithm", action = "store_true")
		self.args = parser.parse_args()

		# Set up ESPARGOS pool and backlog
		hosts = self.args.hosts.split(",")
		self.pool = espargos.Pool([espargos.Board(host) for host in hosts])
		self.pool.start()
		self.pool.calibrate(duration = 2, per_board=False)
		self.backlog = espargos.CSIBacklog(self.pool, enable_ht40=not self.args.l20, size = self.args.backlog)
		self.backlog.start()

		# Qt setup
		self.aboutToQuit.connect(self.onAboutToQuit)
		self.engine = PyQt6.QtQml.QQmlApplicationEngine()
		data_shape = self.backlog.get_csi().shape
		self.sensor_count = data_shape[1] * data_shape[2] * data_shape[3]
		self.subcarrier_count = data_shape[4]
		self.subcarrier_range = np.arange(-self.subcarrier_count // 2, self.subcarrier_count // 2)

	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def sensorCount(self):
		return np.prod(self.pool.get_shape())

	@PyQt6.QtCore.pyqtProperty(list, constant=True)
	def subcarrierRange(self):
		return self.subcarrier_range.tolist()

	def exec(self):
		context = self.engine.rootContext()
		context.setContextProperty("backend", self)

		qmlFile = pathlib.Path(__file__).resolve().parent / "instantaneous-csi-ui.qml"
		self.engine.load(qmlFile.as_uri())
		if not self.engine.rootObjects():
			return -1

		return super().exec()

	# list parameters contain PyQt6.QtCharts.QLineSeries
	@PyQt6.QtCore.pyqtSlot(list, list, PyQt6.QtCharts.QValueAxis)
	def updateCSI(self, powerSeries, phaseSeries, axis):

		csi_backlog = self.backlog.get_csi()
		if not self.args.l20:
			# Fill "gap" in subcarriers with interpolated data
			espargos.util.interpolate_ht40_gap(csi_backlog)


		csi_shifted = espargos.util.shift_to_firstpeak(csi_backlog) if self.args.shift_peak else csi_backlog

		# TODO: If using per-board calibration, interpolation should also be per-board
		csi_interp = espargos.util.csi_interp_iterative(csi_shifted, iterations = 5)
		csi_flat = np.reshape(csi_interp, (-1, csi_interp.shape[-1]))

		if self.args.mvdr or self.args.music:
			if self.args.music:
				superres_delays, superres_pdps = espargos.util.fdomain_to_tdomain_pdp_music(csi_backlog)
			else:
				superres_delays, superres_pdps = espargos.util.fdomain_to_tdomain_pdp_mvdr(csi_backlog)

			superres_pdps_flat = np.reshape(superres_pdps, (-1, superres_pdps.shape[-1]))

			superres_pdps_flat = superres_pdps_flat / np.max(superres_pdps_flat)
			axis.setMin(0)
			axis.setMax(1.1)

			for pwr_series, mvdr_pdp in zip(powerSeries, superres_pdps_flat):
				pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(superres_delays, mvdr_pdp)])
		elif self.args.timedomain:
			zero_padding = np.zeros((csi_flat.shape[0], csi_flat.shape[1] * (self.args.oversampling - 1)), dtype = np.complex64)
			csi_flat = np.concatenate((csi_flat, zero_padding), axis = 1)
			csi_flat = np.fft.fftshift(np.fft.ifft(np.fft.fftshift(csi_flat, axes = -1), axis = -1), axes = -1)
			subcarrier_range_zeropadded = np.arange(-csi_flat.shape[-1] // 2, csi_flat.shape[-1] // 2) / self.args.oversampling
			csi_power = np.abs(csi_flat)
			axis.setMin(0)
			axis.setMax(csi_flat.shape[-1] / np.sqrt(2) / self.args.oversampling**2)
			csi_phase = np.angle(csi_flat * np.exp(-1.0j * np.angle(csi_flat[0, len(csi_flat[0]) // 2])))

			for pwr_series, phase_series, ant_pwr, ant_phase in zip(powerSeries, phaseSeries, csi_power, csi_phase):
				pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range_zeropadded, ant_pwr)])
				phase_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range_zeropadded, ant_phase)])
		else:
			csi_power = 20 * np.log10(np.abs(csi_flat) + 0.00001)
			axis.setMin(10)
			axis.setMax(45)
			csi_phase = np.angle(csi_flat * np.exp(-1.0j * np.angle(csi_flat[0, csi_flat.shape[1] // 2])))

			for pwr_series, phase_series, ant_pwr, ant_phase in zip(powerSeries, phaseSeries, csi_power, csi_phase):
				pwr_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(self.subcarrier_range, ant_pwr)])
				phase_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(self.subcarrier_range, ant_phase)])

	def onAboutToQuit(self):
		self.pool.stop()
		self.backlog.stop()
		self.engine.deleteLater()

	@PyQt6.QtCore.pyqtProperty(bool, constant=True)
	def timeDomain(self):
		return self.args.timedomain

	@PyQt6.QtCore.pyqtProperty(bool, constant=True)
	def superResolution(self):
		return self.args.mvdr or self.args.music


app = EspargosDemoInstantaneousCSI(sys.argv)
sys.exit(app.exec())