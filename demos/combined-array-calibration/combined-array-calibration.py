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

class EspargosDemoCombinedArrayCalibration(PyQt6.QtWidgets.QApplication):
	def __init__(self, argv):
		super().__init__(argv)

		# Parse command line arguments
		parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Combined ESPARGOS arrays calibration")
		parser.add_argument("hosts", type = str, help = "Comma-separated list of host addresses (IP or hostname) of ESPARGOS controllers making up combined array")
		parser.add_argument("-c", "--color-by-sensor-index", default = False, help = "Color by sensor index *within* same board, not by board index", action = "store_true")
		parser.add_argument("-u", "--update-rate", type = float, default = 0.01, help = "Rate by which calibration values are updated in exponential decay filter")
		parser.add_argument("-b", "--boardwise", default = False, help = "Do not calibrate per sensor, calibrate only per board", action = "store_true")
		parser.add_argument("-o", "--outfile", type = str, default = "", help = "File to write additional calibration results to")
		parser.add_argument("-l", "--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
		self.args = parser.parse_args()

		# Set up ESPARGOS pool
		self.pool = espargos.Pool([espargos.Board(host) for host in self.args.hosts.split(",")])
		self.pool.start()
		self.pool.calibrate(duration = 3, per_board = False)
		self.pool.add_csi_callback(self.onCSI)

		if self.args.lltf:
			self.subcarrier_count = espargos.csi.csi_buf_t.lltf.size // 2
		else:
			self.subcarrier_count = (espargos.csi.csi_buf_t.htltf_lower.size + espargos.csi.HT40_GAP_SUBCARRIERS * 2 + espargos.csi.csi_buf_t.htltf_higher.size) // 2
		self.subcarrier_range = np.arange(-self.subcarrier_count // 2, self.subcarrier_count // 2)

		self.poll_timer = PyQt6.QtCore.QTimer(self)
		self.poll_timer.timeout.connect(self.poll_csi)
		self.poll_timer.start(10)

		# Calibration setup
		self.calibration_values = None

		# Qt setup
		self.aboutToQuit.connect(self.onAboutToQuit)
		self.engine = PyQt6.QtQml.QQmlApplicationEngine()

	def exec(self):
		context = self.engine.rootContext()
		context.setContextProperty("backend", self)

		qmlFile = pathlib.Path(__file__).resolve().parent / "combined-array-calibration-ui.qml"
		self.engine.load(qmlFile.as_uri())
		if not self.engine.rootObjects():
			return -1

		return super().exec()

	def poll_csi(self):
		self.pool.run()

	def onCSI(self, clustered_csi):
		if not self.args.lltf and not clustered_csi.is_ht40():
			print("Expected HT40 frame, but got non-HT40 frame, ignoring!")
			return

		# Store CSI if applicable
		sensor_timestamps_raw = clustered_csi.get_sensor_timestamps()
		csi = clustered_csi.deserialize_csi_lltf() if self.args.lltf else clustered_csi.deserialize_csi_ht40()
		assert(self.pool.get_calibration() is not None)
		if self.args.lltf:
			csi = self.pool.get_calibration().apply_lltf(csi, sensor_timestamps_raw)
			espargos.util.interpolate_lltf_gap(csi)
		else:
			csi = self.pool.get_calibration().apply_ht40(csi, sensor_timestamps_raw)
			espargos.util.interpolate_ht40_gap(csi)

		if self.calibration_values is None:
			self.calibration_values = csi
		else:
			csi_to_interpolate = np.asarray([csi, self.calibration_values])
			weights = np.asarray([self.args.update_rate, 1.0 - self.args.update_rate])
			self.calibration_values = espargos.util.csi_interp_iterative(csi_to_interpolate, weights)

	@PyQt6.QtCore.pyqtSlot(list)
	def updateCalibrationResult(self, phaseSeries):
		if self.calibration_values is not None:
			csi = self.calibration_values
			if self.args.boardwise:
				csi = np.sum(csi, axis = (1, 2))
			csi_flat = np.reshape(csi, (-1, csi.shape[-1]))
			csi_phase = np.angle(csi_flat * np.exp(-1.0j * np.angle(csi_flat[0, csi_flat.shape[1] // 2])))

			assert(len(phaseSeries) == len(csi_phase))
			for phase_series, ant_phase in zip(phaseSeries, csi_phase):
				phase_series.replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(self.subcarrier_range, ant_phase)])

	def onAboutToQuit(self):
		if len(self.args.outfile) > 0:
			np.save(self.args.outfile, self.calibration_values)
		self.pool.stop()
		self.engine.deleteLater()

	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def sensorCount(self):
		return self.pool.get_shape()[0] if self.args.boardwise else np.prod(self.pool.get_shape())
	
	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def sensorCountPerBoard(self):
		return 1 if self.args.boardwise else np.prod(self.pool.get_shape()[1:])

	@PyQt6.QtCore.pyqtProperty(list, constant=True)
	def subcarrierRange(self):
		return self.subcarrier_range.tolist()

	@PyQt6.QtCore.pyqtProperty(bool, constant=True)
	def colorBySensorIndex(self):
		return self.args.color_by_sensor_index


app = EspargosDemoCombinedArrayCalibration(sys.argv)
sys.exit(app.exec())