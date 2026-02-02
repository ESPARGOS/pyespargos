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
		parser.add_argument("hosts", type = str, help = "Comma-separated list of host addresses (IP or hostname) of ESPARGOS controllers")
		parser.add_argument("-b", "--backlog", type = int, default = 20, help = "Number of CSI datapoints to average over in backlog")
		parser.add_argument("-m", "--maxage", type = float, default = 10, help = "Maximum age of CSI datapoints before they are cleared")
		parser.add_argument("-s", "--shift-peak", default = False, help = "Time-shift CSI so that first peaks align", action = "store_true")
		parser.add_argument("-l", "--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
		self.args = parser.parse_args()

		# Set up ESPARGOS pool and backlog
		hosts = self.args.hosts.split(",")
		self.pool = espargos.Pool([espargos.Board(host) for host in hosts])
		self.pool.start()
		self.pool.calibrate(per_board = False, duration = 2)
		fields = ["rssi", "timestamp", "host_timestamp", "mac"]
		if self.args.lltf:
			fields.append("lltf")
		else:
			enable.extend(["ht40", "ht20"])
		self.backlog = espargos.CSIBacklog(self.pool, size = self.args.backlog, fields = enable)
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
			csi_key = "lltf" if self.args.lltf else "ht40"
			csi_backlog = self.backlog.get(csi_key)
			timestamp = self.backlog.get_latest("host_timestamp") - self.startTimestamp

			csi_shifted = espargos.util.shift_to_firstpeak_sync(csi_backlog) if self.args.shift_peak else csi_backlog
			csi_interp = espargos.util.csi_interp_iterative(csi_shifted)
			csi_flat = np.reshape(csi_interp, (-1, csi_interp.shape[-1]))

			# TODO: Deal with non-synchronized multi-board setup
			csi_by_antenna = espargos.util.csi_interp_iterative(np.transpose(csi_flat))
			offsets_current_angles = np.angle(csi_by_antenna * np.exp(-1.0j * np.angle(csi_by_antenna[0]))).tolist()

			self.updatePhases.emit(timestamp, offsets_current_angles)


	def onAboutToQuit(self):
		self.pool.stop()
		self.backlog.stop()
		self.engine.deleteLater()

	@PyQt6.QtCore.pyqtProperty(float, constant=True)
	def maxCSIAge(self):
		return self.args.maxage

	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def sensorCount(self):
		return np.prod(self.pool.get_shape())

app = EspargosDemoPhasesOverTime(sys.argv)
sys.exit(app.exec())