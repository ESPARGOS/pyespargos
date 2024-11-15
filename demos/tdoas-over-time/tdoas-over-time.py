#!/usr/bin/env python3

import pathlib
import sys

import espargos.constants

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import numpy as np
import espargos
import argparse
import time

import PyQt6.QtWidgets
import PyQt6.QtCharts
import PyQt6.QtCore
import PyQt6.QtQml

class EspargosDemoTDOAOverTime(PyQt6.QtWidgets.QApplication):
	updateTDOAs = PyQt6.QtCore.pyqtSignal(float, list)

	def __init__(self, argv):
		super().__init__(argv)

		# Parse command line arguments
		parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Show time difference of arrival over time (single board)")
		parser.add_argument("hosts", type = str, help = "Comma-separated list of host addresses (IP or hostname) of ESPARGOS controllers")
		parser.add_argument("-b", "--backlog", type = int, default = 20, help = "Number of CSI datapoints to average over in backlog")
		parser.add_argument("-m", "--music", default = False, help = "Use root-MUSIC algorithm to compute more precise ToAs of LoS paths", action = "store_true")
		parser.add_argument("-a", "--maxage", type = float, default = 10, help = "Maximum age of CSI datapoints before they are cleared")
		self.args = parser.parse_args()

		# Set up ESPARGOS pool and backlog
		hosts = self.args.hosts.split(",")
		self.pool = espargos.Pool([espargos.Board(host) for host in hosts])
		self.pool.start()
		self.pool.calibrate(duration = 4, per_board=False)
		self.backlog = espargos.CSIBacklog(self.pool, size = self.args.backlog)
		self.backlog.start()

		# Qt setup
		self.aboutToQuit.connect(self.onAboutToQuit)
		self.engine = PyQt6.QtQml.QQmlApplicationEngine()

		self.startTimestamp = time.time()

	def exec(self):
		context = self.engine.rootContext()
		context.setContextProperty("backend", self)

		qmlFile = pathlib.Path(__file__).resolve().parent / "tdoas-over-time-ui.qml"
		self.engine.load(qmlFile.as_uri())
		if not self.engine.rootObjects():
			return -1

		return super().exec()

	@PyQt6.QtCore.pyqtSlot()
	def update(self):
		if self.backlog.nonempty():
			#timestamps = self.backlog.get_timestamps()
			#tdoas_ns = np.mean(timestamps - np.mean(timestamps, axis = (1, 2, 3))[:,np.newaxis,np.newaxis,np.newaxis], axis = 0) * 1e9

			csi_backlog_ht40 = self.backlog.get_ht40()
			espargos.util.interpolate_ht40_gap(csi_backlog_ht40)

			# Do interpolation "by_array" due to Doppler (destroys TDoA for moving targets otherwise)
			csi_interp_ht40 = espargos.util.csi_interp_iterative_by_array(csi_backlog_ht40, iterations = 5)

			if self.args.music:
				tdoas_ns = espargos.util.estimate_toas_rootmusic(csi_backlog_ht40) * 1e9
			else:
				tdoas_ns = np.angle(np.sum(csi_interp_ht40[...,1:] * np.conj(csi_interp_ht40[...,:-1]), axis = -1)) / (2 * np.pi) / espargos.constants.WIFI_SUBCARRIER_SPACING * 1e9

			mean_rx_timestamp = self.backlog.get_latest_timestamp() - self.startTimestamp

			self.updateTDOAs.emit(mean_rx_timestamp, tdoas_ns.astype(float).flatten().tolist())

	def onAboutToQuit(self):
		self.pool.stop()
		self.backlog.stop()
		self.engine.deleteLater()

	@PyQt6.QtCore.pyqtProperty(float, constant=True)
	def maxCSIAge(self):
		return self.args.maxage

	@PyQt6.QtCore.pyqtProperty(float, constant=True)
	def sensorCount(self):
		return np.prod(self.pool.get_shape())

app = EspargosDemoTDOAOverTime(sys.argv)
sys.exit(app.exec())