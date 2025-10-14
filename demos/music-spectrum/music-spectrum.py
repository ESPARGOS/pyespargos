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

class EspargosDemoMusicSpectrum(PyQt6.QtWidgets.QApplication):
	def __init__(self, argv):
		super().__init__(argv)

		# Parse command line arguments
		parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Show MUSIC angle of arrival spectrum (single board)")
		parser.add_argument("host", type = str, help = "Host address (IP or hostname) of ESPARGOS controller")
		parser.add_argument("-b", "--backlog", type = int, default = 20, help = "Number of CSI datapoints to average over in backlog")
		parser.add_argument("-s", "--shift-peak", default = False, help = "Time-shift CSI to find LoS peak", action = "store_true")
		parser.add_argument("-l", "--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
		self.args = parser.parse_args()

		# Set up ESPARGOS pool and backlog
		self.pool = espargos.Pool([espargos.Board(self.args.host)])
		self.pool.start()
		self.pool.calibrate(duration = 2)
		self.backlog = espargos.CSIBacklog(self.pool, size = self.args.backlog, enable_lltf=self.args.lltf, enable_ht40=not self.args.lltf)
		self.backlog.start()

		# Qt setup
		self.aboutToQuit.connect(self.onAboutToQuit)
		self.engine = PyQt6.QtQml.QQmlApplicationEngine()

		# Initialize MUSIC scanning angles, steering vectors, ...
		self.scanning_angles = np.linspace(-np.pi / 2, np.pi / 2, 180)
		self.steering_vectors = np.exp(-1.0j * np.outer(np.pi * np.sin(self.scanning_angles), np.arange(espargos.constants.ANTENNAS_PER_ROW)))
		self.spatial_spectrum = None

	def exec(self):
		context = self.engine.rootContext()
		context.setContextProperty("backend", self)

		qmlFile = pathlib.Path(__file__).resolve().parent / "music-spectrum-ui.qml"
		self.engine.load(qmlFile.as_uri())
		if not self.engine.rootObjects():
			return -1

		return super().exec()

	@PyQt6.QtCore.pyqtSlot(PyQt6.QtCharts.QLineSeries, PyQt6.QtCharts.QValueAxis)
	def updateSpatialSpectrum(self, series, axis):
		self.backlog.read_start()
		csi_backlog = self.backlog.get_lltf() if self.args.lltf else self.backlog.get_ht40()
		rssi_backlog = self.backlog.get_rssi()
		self.backlog.read_finish()

		# Weight CSI data with RSSI
		csi_backlog = csi_backlog * 10**(rssi_backlog[..., np.newaxis] / 20)

		# Shift to first peak if requested
		csi_shifted = espargos.util.shift_to_firstpeak(csi_backlog, peak_threshold = 0.5) if self.args.shift_peak else csi_backlog

		# Compute array covariance matrix R over all backlog datapoints, all rows and all subcarriers
		# TODO: Instead of just using all subcarriers to estimate R, should we extract the LoS component?
		#csi_backlog_tdomain = np.fft.fftshift(np.fft.fft(np.fft.fftshift(csi_backlog, axes = -1), axis = -1), axes = -1)
		#print(np.mean(np.abs(csi_backlog_tdomain), axis = (0, 1, 2, 3)))
		csi_shifted_los = np.sum(csi_shifted, axis = -1)
		R = np.einsum("dbri,dbrj->ij", csi_shifted_los, np.conj(csi_shifted_los))
		#R = np.einsum("dbris,dbrjs->ij", csi_shifted, np.conj(csi_shifted))
		eig_val, eig_vec = np.linalg.eig(R)
		order = np.argsort(eig_val)[::-1]
		Qn = eig_vec[:,order][:,1:]
		spatial_spectrum_linear = 1 / np.linalg.norm(np.einsum("ae,ra->er", np.conj(Qn), self.steering_vectors), axis = 0)
		spatial_spectrum_log = 20 * np.log10(spatial_spectrum_linear)

		axis.setMin(np.min(spatial_spectrum_log) - 1)
		axis.setMax(max(np.max(spatial_spectrum_log), axis.max()))

		data = [PyQt6.QtCore.QPointF(np.rad2deg(angle), power) for angle, power in zip(self.scanning_angles, spatial_spectrum_log)]
		series.replace(data)

	def onAboutToQuit(self):
		self.pool.stop()
		self.backlog.stop()
		self.engine.deleteLater()

	@PyQt6.QtCore.pyqtProperty(list, constant=True)
	def scanningAngles(self):
		return self.scanning_angles.tolist()

app = EspargosDemoMusicSpectrum(sys.argv)
sys.exit(app.exec())