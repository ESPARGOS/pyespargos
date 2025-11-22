#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import matplotlib.colors
import espargos.constants
import videocamera
import numpy as np
import espargos
import argparse
import time

import PyQt6.QtMultimedia
import PyQt6.QtWidgets
import PyQt6.QtCharts
import PyQt6.QtCore
import PyQt6.QtQml

# Basic notes:
# Fundamentally, three different kinds of overlays are supported:
# * Option 1: MUSIC spatial spectrum
#   We pass vertical and horizontal spatial spectrum separately to the shader
#   which takes care of visualization.
# * Option 2: Beamspace via FFT
#   We tranform the measured CSI into beamspace by applying a 2D FFT.
#   We pass a 2-dimensional texture to the shader which overlays it on top of the camera image.
#   Since the steering vectors of a UPA cannot be written as the Kronecker product of azimuth and elevation steering vectors,
#   this does not transform antenna space to azimuth / elevation space correctly. Instead, all the valid beams are inside
#   a circle of this FFT beamspace. The shader fixes this distortion by mapping all pixels into this circle.
# * Option 3: Azimuth / elevation space via 2D steering vectors
#   We compute the 2D steering vectors for all valid beams and compute the received power per beam, straightforward.
#   We pass a 2-dimensional texture to the shader which overlays it on top of the camera image, where x is azimuth and y is elevation.    

class EspargosDemoCamera(PyQt6.QtWidgets.QApplication):
	rssiChanged = PyQt6.QtCore.pyqtSignal(float)
	activeAntennasChanged = PyQt6.QtCore.pyqtSignal(float)
	beamspacePowerImagedataChanged = PyQt6.QtCore.pyqtSignal(list)
	recentMacsChanged = PyQt6.QtCore.pyqtSignal(list)

	def __init__(self, argv):
		super().__init__(argv)

		# Parse command line arguments
		parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Overlay received power on top of camera image")
		parser.add_argument("conf", type = str, help = "Path to config file")
		parser.add_argument("-b", "--backlog", type = int, default = 20, help = "Number of CSI datapoints to average over in backlog")
		parser.add_argument("-c", "--camera-index", type = int, help = "Index of the camera, if multiple cameras are available")
		parser.add_argument("-d", "--colorize-delay", default = False, help = "Visualize delay of beamspace components using colors", action = "store_true")
		parser.add_argument("-i", "--no-interpolation", default = False, help = "Do not use datapoint interpolation to reduce computational complexity (can slightly improve appearance)", action = "store_true")
		parser.add_argument("-ra", "--resolution-azimuth", type = int, default = 64, help = "Beamspace resolution for azimuth angle")
		parser.add_argument("-re", "--resolution-elevation", type = int, default = 32, help = "Beamspace resolution for elevation angle")
		parser.add_argument("-fa", "--fov-azimuth", type = int, default = 72, help = "Camera field of view in azimuth direction")
		parser.add_argument("-fe", "--fov-elevation", type = int, default = 41, help = "Camera field of view in elevation direction")
		parser.add_argument("-md", "--max-delay", type = float, default = 0.2, help = "Maximum delay in samples for colorizing delay")
		parser.add_argument("-a", "--additional-calibration", type = str, default = "", help = "File to read additional phase calibration results from")
		parser.add_argument("-l", "--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
		parser.add_argument("-e", "--manual-exposure", default = False, help = "Use manual exposure / brightness control for WiFi overlay", action = "store_true")
		parser.add_argument("--mac-filter", type = str, default = "", help = "Only display CSI data from given MAC address")
		parser.add_argument("--max-age", type = float, default = 0.0, help = "Limit maximum age of CSI data to this value (in seconds). Set to 0.0 to disable.")
		parser.add_argument("--raw-beamspace", default = False, help = "Display raw beamspace data instead of camera overlay", action = "store_true")
		parser.add_argument("--raw-power", default = False, help = "Display raw beamspace power data instead of processed version", action = "store_true")
		parser.add_argument("--csi-completion-timeout", type = float, default = 0.2, help = "Time after which CSI cluster is considered complete even if not all antennas have provided data. Set to zero to disable processing incomplete clusters.")
		parser.add_argument("--mac-list", default = False, help = "Display list of MAC addresses of available transmitters", action = "store_true")
		display_group = parser.add_mutually_exclusive_group()
		display_group.add_argument("-f", "--no-beamspace-fft", default = False, help = "Do NOT compute beamspace via FFT, but use steering vectors (usually slower)", action = "store_true")
		display_group.add_argument("-m", "--music", default = False, help = "Display spatial spectrum computed via MUSIC algorithm", action = "store_true")
		self.args = parser.parse_args()

		# Load config file
		self.indexing_matrix, board_names_hosts, cable_lengths, cable_velocity_factors, self.n_rows, self.n_cols = espargos.util.parse_combined_array_config(self.args.conf)

		# Load additional calibration data from file, if provided
		self.additional_calibration = None
		if len(self.args.additional_calibration) > 0:
			self.additional_calibration = np.load(self.args.additional_calibration)

		# Set up ESPARGOS pool and backlog
		self.pool = espargos.Pool([espargos.Board(host) for host in board_names_hosts.values()])
		self.pool.start()
		self.pool.calibrate(duration = 3, per_board = False, cable_lengths = cable_lengths, cable_velocity_factors = cable_velocity_factors)
		self.backlog = espargos.CSIBacklog(self.pool, size = self.args.backlog, enable_lltf = self.args.lltf, enable_ht40 = not self.args.lltf, cb_predicate = self._cb_predicate)
		self.backlog.set_mac_filter("^" + self.args.mac_filter.replace(":", "").replace("-", ""))
		self.backlog.start()

		# Qt setup
		self.aboutToQuit.connect(self.onAboutToQuit)
		self.engine = PyQt6.QtQml.QQmlApplicationEngine()

		# Camera setup
		self.videocamera = videocamera.VideoCamera(self.args.camera_index)

		# ESPARGOS is a UPA with half-wavelength antenna separation
		self.scanning_angles = np.linspace(np.deg2rad(-90), np.deg2rad(90), 128)
		self.k = np.pi * np.sin(self.scanning_angles)
		self.spatial_spectra_db = dict()

		# Pre-compute 2d steering vectors to avoid recomputation
		self.scanning_angles_azi = np.linspace(np.deg2rad(-90), np.deg2rad(90), self.args.resolution_azimuth)
		self.scanning_angles_ele = np.linspace(np.deg2rad(-90), np.deg2rad(90), self.args.resolution_elevation)
		self.k_c = -np.pi * np.cos(self.scanning_angles_ele)[np.newaxis,:] * np.sin(self.scanning_angles_azi)[:,np.newaxis]
		self.k_r = -np.pi * np.sin(self.scanning_angles_ele)

		# phase_c depends on both elevation and azimuth angle and has shape (columns, azimuth angle, elevation angle)
		# phase_r only depends on elevation angle and has shape (rows, elevation angle)
		# steering_vectors_2d has shape (rows, columns, azimuth angle, elevation angle)
		antenna_index_c = np.arange(self.n_cols)
		antenna_index_r = np.arange(self.n_rows)
		phase_c = antenna_index_c[:,np.newaxis,np.newaxis] * self.k_c[np.newaxis,:,:]
		phase_r = antenna_index_r[:,np.newaxis] * self.k_r[np.newaxis,:]

		self.steering_vectors_2d = np.exp(1.0j * (phase_c[np.newaxis,:,:,:] + phase_r[:,np.newaxis,np.newaxis,:]))

		# Manual exposure control (only used if manual exposure is enabled)
		self.exposure = 0

		# Statistics display
		self.mean_rssi = -np.inf
		self.mean_active_antennas = 0

		# List of recent MAC addresses
		self.recent_macs = set()

	def exec(self):
		context = self.engine.rootContext()
		context.setContextProperty("backend", self)
		context.setContextProperty("WebCam", self.videocamera)

		qmlFile = pathlib.Path(__file__).resolve().parent / "camera-ui.qml"
		self.engine.load(qmlFile.as_uri())
		if not self.engine.rootObjects():
			return -1

		# disable auto-focus and enable camera stream
		self.videocamera.setFocusMode(PyQt6.QtMultimedia.QCamera.FocusMode.FocusModeManual)
		self.videocamera.start()

		return super().exec()

	@PyQt6.QtCore.pyqtSlot()
	def updateSpatialSpectrum(self):
		self.backlog.read_start()
		csi_backlog = self.backlog.get_lltf() if self.args.lltf else self.backlog.get_ht40()
		rssi_backlog = self.backlog.get_rssi()
		timestamp_backlog = self.backlog.get_timestamps()
		mac_backlog = self.backlog.get_macs()
		self.backlog.read_finish()
		mean_timestamp_backlog = np.nanmean(timestamp_backlog, axis = (1, 2, 3))

		if self.args.max_age > 0.0:
			csi_backlog[mean_timestamp_backlog < (time.time() - self.args.max_age),...] = 0
			recent_rssi_backlog = rssi_backlog[mean_timestamp_backlog > (time.time() - self.args.max_age),...]
		else:
			recent_rssi_backlog = rssi_backlog

		# Update mean RSSI
		self.mean_rssi = 10 * np.log10(np.nanmean(10**(recent_rssi_backlog / 10)) + 1e-6) if recent_rssi_backlog.size > 0 else -np.inf
		self.rssiChanged.emit(self.mean_rssi)

		# Update mean number of active antennas
		if recent_rssi_backlog.shape[0] > 0:
			self.mean_active_antennas = np.prod(recent_rssi_backlog.shape[1:]) - np.mean(np.sum(np.isnan(recent_rssi_backlog), axis = (1, 2, 3)))
			self.activeAntennasChanged.emit(self.mean_active_antennas)

		# Update list of recent MAC addresses
		# Only send signal if list of MAC addresses has changed
		# mac_backlog is a numpy array of shape (n_packets, 6) of data type uint8, where each row is a MAC address
		if self.args.mac_list:
			mac_strings = ["{:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}".format(*mac) for mac in mac_backlog]
			mac_strings_set = set(mac_strings)

			# Check if set of stored recent MACs match current MACs exactly, including contents
			if self.recent_macs != mac_strings_set:
				self.recent_macs = mac_strings_set
				self.recentMacsChanged.emit(list(self.recent_macs))

		# CSI backlog may be incomplete: If individual sensor did not provide packet, CSI value is NaN
		# For the purpose of visualization, we treat these NaN values as 0
		csi_backlog = np.nan_to_num(csi_backlog, nan = 0.0)
		rssi_backlog = np.nan_to_num(rssi_backlog, nan = -np.inf)

		espargos.util.remove_mean_sto(csi_backlog)

		# Apply additional calibration (only phase)
		if self.additional_calibration is not None:
			# TODO: espargos.pool should natively support additional calibration
			csi_backlog = np.einsum("dbrcs,brcs->dbrcs", csi_backlog, np.exp(-1.0j * np.angle(self.additional_calibration)))

		# Weight CSI data with RSSI
		csi_backlog = csi_backlog * 10**(rssi_backlog[..., np.newaxis] / 20)

		# Build combined array CSI data and add fake array index dimension
		csi_combined = espargos.util.build_combined_array_csi(self.indexing_matrix, csi_backlog)
		csi_combined = csi_combined[:,np.newaxis,:,:,:]

		# Get rid of gap in CSI data around DC
		if self.args.lltf:
			espargos.util.interpolate_lltf_gap(csi_combined)
		else:
			espargos.util.interpolate_ht40_gap(csi_combined)

		# Shift all CSI datapoints in time so that LoS component arrives at the same time
		csi_combined = espargos.util.shift_to_firstpeak_sync(csi_combined, peak_threshold = (0.4 if self.args.lltf else 0.1))

		# For computational efficiency reasons, reduce number of datapoints to one by interpolating over all datapoints
		if not self.args.no_interpolation:
			csi_combined = np.asarray([espargos.util.csi_interp_iterative(csi_combined, iterations = 5)])

		# Option 1: MUSIC spatial spectrum (simplest)
		if self.args.music:
			# Compute array covariance matrix R over all backlog datapoints, all rows and all subcarriers
			R_h = np.einsum("dbris,dbrjs->ij", csi_combined, np.conj(csi_combined))
			R_v = np.einsum("dbics,dbjcs->ij", csi_combined, np.conj(csi_combined))
			self.spatial_spectra_db["horizontal"] = self._music_algorithm(R_h)
			self.spatial_spectra_db["vertical"] = self._music_algorithm(R_v)
			spatial_spectra_max = np.max(list(self.spatial_spectra_db.values()))
			self.spatial_spectra_db["horizontal"] = self.spatial_spectra_db["horizontal"] - spatial_spectra_max
			self.spatial_spectra_db["vertical"] = self.spatial_spectra_db["vertical"] - spatial_spectra_max
		else:
			# Option 2: Beamspace via FFT
			if not self.args.no_beamspace_fft:
				# Exploit time-domain sparsity to reduce number of 2D FFTs from antenna space to beamspace
				csi_tdomain = np.fft.ifftshift(np.fft.ifft(np.fft.fftshift(csi_combined, axes = -1), axis = -1), axes = -1)
				tap_count = csi_tdomain.shape[-1]
				csi_tdomain_cut = csi_tdomain[...,tap_count//2 + 1 - 16:tap_count//2 + 1 + 17]
				csi_fdomain_cut = np.fft.ifftshift(np.fft.fft(np.fft.fftshift(csi_tdomain_cut, axes = -1), axis = -1), axes = -1)

				# Here, we only go to DFT beamspace, not directly azimuth / elevation space,
				# but the shader can take care of fixing the distortion.
				# csi_zeropadded has shape (datapoints, azimuth / row, elevation / column, subcarriers)				
				csi_zeropadded = np.zeros((csi_fdomain_cut.shape[0], self.args.resolution_azimuth, self.args.resolution_elevation, csi_fdomain_cut.shape[-1]), dtype = csi_fdomain_cut.dtype)
				real_rows_half = csi_fdomain_cut.shape[2] // 2
				real_cols_half = csi_fdomain_cut.shape[3] // 2
				zeropadded_rows_half = csi_zeropadded.shape[2] // 2
				zeropadded_cols_half = csi_zeropadded.shape[1] // 2
				csi_zeropadded[:,zeropadded_cols_half-real_cols_half:zeropadded_cols_half+real_cols_half,zeropadded_rows_half-real_rows_half:zeropadded_rows_half+real_rows_half,:] = np.swapaxes(csi_fdomain_cut[:,0,:,:,:], 1, 2)
				csi_zeropadded = np.fft.ifftshift(csi_zeropadded, axes = (1, 2))
				beam_frequency_space = np.fft.fft2(csi_zeropadded, axes = (1, 2))
				beam_frequency_space = np.fft.fftshift(beam_frequency_space, axes = (1, 2))
			
			# Option 3: Azimuth / elevation space via 2D steering vectors
			else:
				# Compute sum of received power per steering angle over all datapoints and subcarriers
				# real 2d spatial spectrum is too slow...
				# we can use 2D FFT to get to beamspace, which of course is technically not correct
				# (cannot separate 2D steering vector into Kronecker product of azimuth / elevation steering vectors)
				beam_frequency_space = np.einsum("rcae,dbrcs->daes", self.steering_vectors_2d, csi_combined, optimize = True)

			if self.args.raw_power:
				db_beamspace = 10 * np.log10(np.sum(np.abs(beam_frequency_space)**2, axis=(0, 3)))
				db_beamspace_norm = (db_beamspace - np.max(db_beamspace) + 15) / 15
				db_beamspace_norm = np.clip(db_beamspace_norm, 0, 1)
				color_beamspace = self._viridis(db_beamspace_norm)
			
				alpha_channel = np.ones((*color_beamspace.shape[:2], 1))
				color_beamspace_rgba = np.clip(np.concatenate((color_beamspace, alpha_channel), axis=-1), 0, 1)
				self.beamspace_power_imagedata = np.asarray(np.swapaxes(color_beamspace_rgba, 0, 1).ravel() * 255, dtype = np.uint8)
			else:
				power_beamspace = np.sum(np.abs(beam_frequency_space)**2, axis=(0, 3))
				power_visualization_beamspace = power_beamspace**3

				if self.args.manual_exposure:
					color_value = power_visualization_beamspace / (10 ** ((1 - self.exposure) / 0.1) + 1e-8)
				else:
					color_value = power_visualization_beamspace / (np.max(power_visualization_beamspace) + 1e-6)

				if self.args.colorize_delay:
					# Compute beam powers and delay. Beam power is value, delay is hue.
					beamspace_weighted_delay_phase = np.sum(beam_frequency_space[...,1:] * np.conj(beam_frequency_space[...,:-1]), axis=(0, 3))
					delay_by_beam = np.angle(beamspace_weighted_delay_phase)
					mean_delay = np.angle(np.sum(beamspace_weighted_delay_phase))

					hsv = np.zeros((beam_frequency_space.shape[1], beam_frequency_space.shape[2], 3))
					hsv[:,:,0] = (np.clip((delay_by_beam - mean_delay) / self.args.max_delay, 0, 1) + 1/3) % 1.0
					hsv[:,:,1] = 0.8
					hsv[:,:,2] = color_value

					wifi_image_rgb = matplotlib.colors.hsv_to_rgb(hsv)
					alpha_channel = np.ones((*wifi_image_rgb.shape[:2], 1))
					wifi_image_rgba = np.clip(np.concatenate((wifi_image_rgb, alpha_channel), axis=-1), 0, 1)
					self.beamspace_power_imagedata = np.asarray(np.swapaxes(wifi_image_rgba, 0, 1).ravel() * 255, dtype = np.uint8)
				else:
					self.beamspace_power = np.sum(np.abs(beam_frequency_space)**2, axis = (0, 3))
					self.beamspace_power_imagedata = np.zeros(4 * self.beamspace_power.size, dtype = np.uint8)
					self.beamspace_power_imagedata[1::4] = np.clip(np.swapaxes(color_value, 0, 1).ravel(), 0, 1) * 255
					self.beamspace_power_imagedata[3::4] = 255

			self.beamspacePowerImagedataChanged.emit(self.beamspace_power_imagedata.tolist())

	def _music_algorithm(self, R):
		steering_vectors = np.exp(1.0j * np.outer(self.k, np.arange(R.shape[0])))

		# Compute spatial spectrum using MUSIC algorithm based on R
		eig_val, eig_vec = np.linalg.eig(R)
		order = np.argsort(eig_val)[::-1]
		Qn = eig_vec[:,order][:,1:]
		spatial_spectrum = 1 / np.linalg.norm(np.einsum("ae,ra->er", np.conj(Qn), steering_vectors), axis = 0)
		spatial_spectrum /= 2

		return 20 * np.log10(spatial_spectrum)
	
	def _viridis(self, values):
		viridis_colormap = np.asarray([
			(0.267004, 0.004874, 0.329415),
			(0.229739, 0.322361, 0.545706),
			(0.127568, 0.566949, 0.550556),
			(0.369214, 0.788888, 0.382914),
			(0.993248, 0.906157, 0.143936),
			(0.993248, 0.906157, 0.143936)
		])

		n = len(viridis_colormap) - 1
		idx = values * n
		low = np.floor(idx).astype(int)
		high = np.ceil(idx).astype(int)
		t = idx - low

		c0 = viridis_colormap[low]
		c1 = viridis_colormap[high]

		return c0 * (1 - t[:,:,np.newaxis]) + c1 * t[:,:,np.newaxis]

	def _cb_predicate(self, csi_completion_state, csi_age):
		timeout_condition = False
		if self.args.csi_completion_timeout > 0:
			timeout_condition = np.sum(csi_completion_state) >= 2 and csi_age > self.args.csi_completion_timeout

		return np.all(csi_completion_state) or timeout_condition

	def onAboutToQuit(self):
		self.videocamera.stop()
		self.pool.stop()
		self.backlog.stop()
		self.engine.deleteLater()

	@PyQt6.QtCore.pyqtProperty(list, constant=True)
	def scanningAngles(self):
		return self.scanning_angles.tolist()
	
	@PyQt6.QtCore.pyqtProperty(list)
	def horizontalSpectrum(self):
		try:
			spectrum = self.spatial_spectra_db["horizontal"]
		except:
			spectrum = -np.inf * np.ones(self.scanning_angles.shape[0])
		return spectrum.tolist()

	@PyQt6.QtCore.pyqtProperty(list)
	def verticalSpectrum(self):
		try:
			spectrum = self.spatial_spectra_db["vertical"]
		except:
			spectrum = -np.inf * np.ones(self.scanning_angles.shape[0])
		return spectrum.tolist()
	
	@PyQt6.QtCore.pyqtProperty(bool, constant=True)
	def music(self):
		return self.args.music

	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def resolutionAzimuth(self):
		return self.args.resolution_azimuth

	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def resolutionElevation(self):
		return self.args.resolution_elevation

	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def fovAzimuth(self):
		return self.args.fov_azimuth
	
	@PyQt6.QtCore.pyqtProperty(int, constant=True)
	def fovElevation(self):
		return self.args.fov_elevation

	@PyQt6.QtCore.pyqtProperty(bool, constant=True)
	def isFFTBeamspace(self):
		return not self.args.no_beamspace_fft

	@PyQt6.QtCore.pyqtProperty(bool, constant=True)
	def manualExposure(self):
		return self.args.manual_exposure

	@PyQt6.QtCore.pyqtSlot(float)
	def adjustExposure(self, exposure):
		self.exposure = exposure

	@PyQt6.QtCore.pyqtProperty(bool, constant=True)
	def rawBeamspace(self):
		return self.args.raw_beamspace

	@PyQt6.QtCore.pyqtProperty(float, constant=False, notify = rssiChanged)
	def rssi(self):
		return self.mean_rssi

	@PyQt6.QtCore.pyqtProperty(float, constant=False, notify = activeAntennasChanged)
	def activeAntennas(self):
		return self.mean_active_antennas
	
	@PyQt6.QtCore.pyqtProperty(bool, constant=True)
	def macListEnabled(self):
		return self.args.mac_list
	
	@PyQt6.QtCore.pyqtProperty(list, constant=False, notify = recentMacsChanged)
	def macList(self):
		return self.recent_macs
	
	@PyQt6.QtCore.pyqtSlot(str)
	def setMacFilter(self, mac):
		self.pool.set_mac_filter(mac)

	@PyQt6.QtCore.pyqtSlot()
	def clearMacFilter(self):
		self.pool.clear_mac_filter()

app = EspargosDemoCamera(sys.argv)
sys.exit(app.exec())
