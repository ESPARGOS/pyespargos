#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import numpy as np
import matplotlib
import espargos
import argparse
import yaml

import PyQt6.QtWidgets
import PyQt6.QtCharts
import PyQt6.QtCore
import PyQt6.QtQml

class EspargosDemoCombinedArray(PyQt6.QtWidgets.QApplication):
	updateColors = PyQt6.QtCore.pyqtSignal(list)

	def __init__(self, argv):
		super().__init__(argv)

		# Parse command line arguments
		parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Combined ESPARGOS arrays")
		parser.add_argument("conf", type = str, help = "Path to config file")
		parser.add_argument("-b", "--backlog", type = int, default = 20, help = "Number of CSI datapoints to average over in backlog")
		parser.add_argument("-l", "--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
		self.args = parser.parse_args()

		# Load config file
		self.indexing_matrix, board_names_hosts, cable_lengths, cable_velocity_factors, self.n_rows, self.n_cols = espargos.util.parse_combined_array_config(self.args.conf)

		# Set up ESPARGOS pool and backlog
		self.pool = espargos.Pool([espargos.Board(host) for host in board_names_hosts.values()])
		self.pool.start()
		self.pool.calibrate(duration = 4, per_board = False, cable_lengths = cable_lengths, cable_velocity_factors = cable_velocity_factors)
		self.backlog = espargos.CSIBacklog(self.pool, size = self.args.backlog, enable_lltf = self.args.lltf, enable_ht40 = not self.args.lltf)
		self.backlog.start()

		# Qt setup
		self.aboutToQuit.connect(self.onAboutToQuit)
		self.engine = PyQt6.QtQml.QQmlApplicationEngine()

	def exec(self):
		context = self.engine.rootContext()
		context.setContextProperty("backend", self)

		qmlFile = pathlib.Path(__file__).resolve().parent / "combined-array-ui.qml"
		self.engine.load(qmlFile.as_uri())
		if not self.engine.rootObjects():
			return -1

		return super().exec()

	@PyQt6.QtCore.pyqtSlot()
	def updateRequest(self):
		self.backlog.read_start()
		csi_backlog = self.backlog.get_lltf() if self.args.lltf else self.backlog.get_ht40()
		self.backlog.read_finish()

		csi_largearray = espargos.util.build_combined_array_csi(self.indexing_matrix, csi_backlog)

		R = np.einsum("dnis,dmjs->nimj", csi_largearray, np.conj(csi_largearray))
		R = np.reshape(R, (R.shape[0] * R.shape[1], R.shape[2] * R.shape[3]))
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

	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def numberOfRows(self):
		return self.n_rows

	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def numberOfColumns(self):
		return self.n_cols

app = EspargosDemoCombinedArray(sys.argv)
sys.exit(app.exec())