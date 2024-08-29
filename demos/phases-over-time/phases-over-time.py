#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import numpy as np
import espargos
import argparse
import time

import PyQt6.QtWidgets
import PyQt6.QtCharts
import PyQt6.QtCore
import PyQt6.QtQml

class EspargosDemoPhasesOverTime(PyQt6.QtWidgets.QApplication):
	updatePhases = PyQt6.QtCore.pyqtSignal(float, list)

	def __init__(self, argv):
		super().__init__(argv)

		# Parse command line arguments
		parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Show phases over time (single board)")
		parser.add_argument("host", type = str, help = "Host address (IP or hostname) of ESPARGOS controller")
		parser.add_argument("-b", "--backlog", type = int, default = 20, help = "Number of CSI datapoints to average over in backlog")
		parser.add_argument("-m", "--maxage", type = float, default = 10, help = "Maximum age of CSI datapoints before they are cleared")
		parser.add_argument("-s", "--shift-peak", default = False, help = "Time-shift CSI so that first peaks align", action = "store_true")
		self.args = parser.parse_args()

		# Set up ESPARGOS pool and backlog
		self.pool = espargos.Pool([espargos.Board(self.args.host)])
		self.pool.start()
		self.pool.calibrate(duration = 2)
		self.backlog = espargos.CSIBacklog(self.pool, size = self.args.backlog)
		self.backlog.add_update_callback(self.update)
		self.backlog.start()

		# Qt setup
		self.aboutToQuit.connect(self.onAboutToQuit)
		self.engine = PyQt6.QtQml.QQmlApplicationEngine()

		self.startTimestamp = time.time()

	def exec(self):
		context = self.engine.rootContext()
		context.setContextProperty("backend", self)

		qmlFile = pathlib.Path(__file__).resolve().parent / "phases-over-time-ui.qml"
		self.engine.load(qmlFile.as_uri())
		if not self.engine.rootObjects():
			return -1

		return super().exec()

	def update(self):
		if self.backlog.nonempty():
			csi_backlog_ht40 = self.backlog.get_ht40()
			csi_ht40_shifted = espargos.util.shift_to_firstpeak(csi_backlog_ht40) if self.args.shift_peak else csi_backlog_ht40
			csi_interp_ht40 = espargos.util.csi_interp_iterative(csi_ht40_shifted)
			csi_flat = np.reshape(csi_interp_ht40, (-1, csi_interp_ht40.shape[-1]))

			csi_by_antenna = espargos.util.csi_interp_iterative(np.transpose(csi_flat))
			timestamp = self.backlog.get_latest_timestamp() - self.startTimestamp
			offsets_current_angles = np.angle(csi_by_antenna * np.exp(-1.0j * np.angle(csi_by_antenna[0]))).tolist()

			self.updatePhases.emit(timestamp, offsets_current_angles)


	def onAboutToQuit(self):
		self.pool.stop()
		self.backlog.stop()
		self.engine.deleteLater()

	@PyQt6.QtCore.pyqtProperty(float, constant=True)
	def maxCSIAge(self):
		return self.args.maxage

app = EspargosDemoPhasesOverTime(sys.argv)
sys.exit(app.exec())