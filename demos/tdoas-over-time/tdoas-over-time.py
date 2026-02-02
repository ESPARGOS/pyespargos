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
		algo_group = parser.add_mutually_exclusive_group()
		algo_group.add_argument("-m", "--music", default = False, help = "Use root-MUSIC algorithm to compute more precise ToAs of LoS paths", action = "store_true")
		algo_group.add_argument("-u", "--unwrap", default = False, help = "Use phase unwrapping algorithm to compute more precise ToAs of LoS paths", action = "store_true")
		parser.add_argument("-a", "--average", default = False, help = "Average TDoAs over all antennas in array", action = "store_true")
		parser.add_argument("-c", "--maxage", type = float, default = 10, help = "Maximum age of CSI datapoints before they are cleared")
		parser.add_argument("-l", "--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
		self.args = parser.parse_args()

		# Set up ESPARGOS pool and backlog
		hosts = self.args.hosts.split(",")
		self.pool = espargos.Pool([espargos.Board(host) for host in hosts])
		self.pool.start()
		self.pool.calibrate(duration = 4, per_board=False)
		enable = ["rssi", "timestamp", "host_timestamp", "mac"]
		if self.args.lltf:
			enable.append("lltf")
		else:
			enable.extend(["ht40", "ht20"])
		self.backlog = espargos.CSIBacklog(self.pool, size = self.args.backlog, enable = enable)
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
			#timestamps = self.backlog.get("timestamp")
			#tdoas_ns = np.mean(timestamps - np.mean(timestamps, axis = (1, 2, 3))[:,np.newaxis,np.newaxis,np.newaxis], axis = 0) * 1e9

			self.backlog.read_start()
			csi_backlog = self.backlog.get("lltf") if self.args.lltf else self.backlog.get("ht40")
			mean_rx_timestamp = self.backlog.get_latest("host_timestamp") - self.startTimestamp
			self.backlog.read_finish()

			if self.args.lltf:
				espargos.util.interpolate_lltf_gap(csi_backlog)
			else:
				espargos.util.interpolate_ht40ltf_gap(csi_backlog)

			# Do interpolation "by_array" due to Doppler (destroys TDoA for moving targets otherwise)
			csi_interp = espargos.util.csi_interp_iterative_by_array(csi_backlog, iterations = 5)

			if self.args.music:
				tdoas_ns = espargos.util.estimate_toas_rootmusic(csi_backlog, per_board_average = self.args.average) * 1e9
			elif self.args.unwrap:
				phases = np.unwrap(np.angle(csi_interp), axis = -1)
				tdoas_ns = (phases[..., -1] - phases[..., 0]) / (2 * np.pi * phases.shape[-1]) / espargos.constants.WIFI_SUBCARRIER_SPACING * 1e9
			else:
				sum_axis = -1 if not self.args.average else (1, 2, 3)
				tdoas_ns = np.angle(np.sum(csi_interp[...,1:] * np.conj(csi_interp[...,:-1]), axis = sum_axis)) / (2 * np.pi) / espargos.constants.WIFI_SUBCARRIER_SPACING * 1e9

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
		return np.prod(self.pool.get_shape()) if not self.args.average else self.pool.get_shape()[0]

app = EspargosDemoTDOAOverTime(sys.argv)
sys.exit(app.exec())