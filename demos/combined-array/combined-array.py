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
		parser.add_argument("--l20", default = False, help = "Operate on 20MHz band", action = "store_true")
		parser.add_argument("-b", "--backlog", type = int, default = 20, help = "Number of CSI datapoints to average over in backlog")
		self.args = parser.parse_args()

		# Parse configuration
		with open(self.args.conf, "r") as conffile:
			self.conf = yaml.safe_load(conffile)

		# Make sure array is square
		self.n_rows = len(self.conf["array"])
		self.n_cols = len(self.conf["array"][0])
		for row in self.conf["array"]:
			assert(len(row) == self.n_cols)

		# Build an indexing matrix. The matrix contains the indices to get from the flattened representation
		# of the CSI of all subarrays to the CSI for the large array.
		self.indexing_matrix = np.zeros((self.n_rows, self.n_cols), dtype = int)
		self.boardnames = list(self.conf["boards"].keys())

		for row in range(self.n_rows):
			for col in range(self.n_cols):
				name, index_row, index_col = self.conf["array"][row][col].split(".")
				offset_board = self.boardnames.index(name) * espargos.constants.ANTENNAS_PER_BOARD
				offset_row = int(index_row) * espargos.constants.ANTENNAS_PER_ROW
				self.indexing_matrix[row, col] = offset_board + offset_row + int(index_col)

		# Get cable lengths and velocity factors
		cable_lengths = np.asarray([board["cable"]["length"] for board in self.conf["boards"].values()])
		cable_velocity_factors = np.asarray([board["cable"]["velocity_factor"] for board in self.conf["boards"].values()])

		# Set up ESPARGOS pool and backlog
		self.pool = espargos.Pool([espargos.Board(board["host"]) for board in self.conf["boards"].values()])
		self.pool.start()
		self.pool.calibrate(duration = 4, per_board = False, cable_lengths = cable_lengths, cable_velocity_factors = cable_velocity_factors)
		self.backlog = espargos.CSIBacklog(self.pool, enable_ht40=not self.args.l20, size = self.args.backlog)
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
		csi_backlog = self.backlog.get_csi()

		csi_shifted = espargos.util.shift_to_firstpeak(csi_backlog)
		csi_by_array_row_col = np.moveaxis(csi_shifted, 0, -1)
		csi_by_antenna = np.reshape(csi_by_array_row_col, (csi_by_array_row_col.shape[0] * csi_by_array_row_col.shape[1] * csi_by_array_row_col.shape[2], csi_by_array_row_col.shape[3], csi_by_array_row_col.shape[4]))
		csi_largearray = csi_by_antenna[self.indexing_matrix]
		csi_largearray = np.moveaxis(csi_largearray, -1, 0)

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