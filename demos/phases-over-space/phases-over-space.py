#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import numpy as np
import matplotlib
import espargos
import argparse

import PyQt6.QtWidgets
import PyQt6.QtCharts
import PyQt6.QtCore
import PyQt6.QtQml

class EspargosDemoPhasesOverSpace(PyQt6.QtWidgets.QApplication):
	updateColors = PyQt6.QtCore.pyqtSignal(list)

	def __init__(self, argv):
		super().__init__(argv)

		# Parse command line arguments
		parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Show phases over space (single board)")
		parser.add_argument("host", type = str, help = "Host address (IP or hostname) of ESPARGOS controller")
		parser.add_argument("--l20", default = False, help = "Operate on 20MHz band", action = "store_true")
		parser.add_argument("-b", "--backlog", type = int, default = 100, help = "Number of CSI datapoints to average over in backlog")
		parser.add_argument("-s", "--shift-peak", default = False, help = "Time-shift CSI so that first peaks align", action = "store_true")
		parser.add_argument("-n", "--no-calibration", default = False, help = "Disable phase calibration", action = "store_true")
		self.args = parser.parse_args()

		# Set up ESPARGOS pool and backlog
		self.pool = espargos.Pool([espargos.Board(self.args.host)])
		self.pool.start()
		if not self.args.no_calibration:
			self.pool.calibrate(duration = 4)
		self.backlog = espargos.CSIBacklog(self.pool, enable_ht40=not self.args.l20, size = self.args.backlog, calibrate = not self.args.no_calibration)
		self.backlog.start()

		# Qt setup
		self.aboutToQuit.connect(self.onAboutToQuit)
		self.engine = PyQt6.QtQml.QQmlApplicationEngine()

	def exec(self):
		context = self.engine.rootContext()
		context.setContextProperty("backend", self)

		qmlFile = pathlib.Path(__file__).resolve().parent / "phases-over-space-ui.qml"
		self.engine.load(qmlFile.as_uri())
		if not self.engine.rootObjects():
			return -1

		return super().exec()

	@PyQt6.QtCore.pyqtSlot()
	def updateRequest(self):
		csi_backlog = self.backlog.get_csi()
		csi_shifted = espargos.util.shift_to_firstpeak(csi_backlog) if self.args.shift_peak else csi_backlog
		R = np.einsum("dbmis,dbnjs->minj", csi_shifted, np.conj(csi_shifted))
		R = np.reshape(R, (8, 8)) # TODO
		w, v = np.linalg.eig(R)
		csi_smoothed = v[:, np.argmax(w)]
		offsets_current = csi_smoothed.flatten()
		phases = np.angle(offsets_current * np.exp(-1.0j * np.angle(offsets_current[0]))).tolist()

		norm = matplotlib.colors.Normalize(vmin = -np.pi, vmax = np.pi, clip = True)
		mapper = matplotlib.cm.ScalarMappable(norm=norm, cmap = "twilight")

		self.updateColors.emit(mapper.to_rgba(phases).tolist())

	def onAboutToQuit(self):
		self.pool.stop()
		self.backlog.stop()
		self.engine.deleteLater()

app = EspargosDemoPhasesOverSpace(sys.argv)
sys.exit(app.exec())