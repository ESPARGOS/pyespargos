#!/usr/bin/env python3

import numpy as np
import yaml

from . import constants
from . import csi


def csi_interp_iterative(csi: np.ndarray, weights: np.ndarray = None, iterations = 10):
	"""
	Interpolates CSI data (frequency-domain or time-domain) using an iterative algorithm.
	Tries to sum up the CSI data phase-coherently with the least error.
	More details about the algorithm (which is quite straightforward) can be found in section
	"IV. Linear Interpolation Baseline" in the paper "GAN-based Massive MIMO Channel Model Trained on Measured Data".

	:param csi: The CSI data to interpolate. Complex-valued NumPy array. Can be an array with arbitrary dimensions, but the first dimension must be the number of CSI datapoints.
	:param weights: The weights to use for each CSI datapoint. If None, all datapoints are weighted equally.
	:param iterations: The number of iterations to perform. Default is 10.

	:return: The interpolated CSI data. Complex-valued NumPy array with the same shape as the input CSI data.
	"""
	if weights is None:
		weights = np.ones(len(csi), dtype = csi.dtype) / len(csi)

	phi = np.zeros_like(weights, dtype = csi.dtype)
	w = None
	
	for i in range(iterations):
		w = np.einsum("n,n,n...->...", weights, np.exp(-1.0j * phi), csi)
		phi = np.angle(np.einsum("a,na->n", np.conj(w.flatten()), csi.reshape(len(csi), -1)))
		#err = np.sum([weights[n] * np.linalg.norm(csi[n] - np.exp(1.0j * phi[n]) * w)**2 for n in range(len(csi))])

	return w

def csi_interp_iterative_by_array(csi: np.ndarray, weights: np.ndarray = None, iterations = 10):
	"""
	Interpolates CSI data (frequency-domain or time-domain) using an iterative algorithm.
	Same as :func:`csi_interp_iterative`, but assumes that second dimension of :code:`csi` is the antenna array dimension and performs the interpolation for each antenna array separately.
	"""
	csi_interp = np.zeros((csi.shape[1], *csi.shape[2:]), dtype = csi.dtype)

	for b in range(csi.shape[1]):
		csi_interp[b] = csi_interp_iterative(csi[:,b], weights=weights, iterations=iterations)

	return csi_interp

def csi_interp_eigenvec(csi: np.ndarray, weights: np.ndarray = None):
	"""
	Interpolates CSI data (frequency-domain or time-domain) by finding the principal eigenvector of the covariance matrix.

	:param csi: The CSI data to interpolate. Complex-valued NumPy array. Can be an array with arbitrary dimensions, but the first dimension must be the number of CSI datapoints.
	:param weights: The weights to use for each CSI datapoint. If None, all datapoints are weighted equally.
	"""
	if weights is None:
		weights = np.ones(len(csi)) / len(csi)

	csi_shape = csi.shape[1:]
	csi = np.reshape(csi, (csi.shape[0], -1))
	R = np.einsum("n,na,nb->ab", weights, csi, np.conj(csi))

	# eig is faster than eigh for small matrices like the one here
	w, v = np.linalg.eig(R)
	principal = np.argmax(w)

	return np.reshape(v[:, principal], csi_shape)

def get_frequencies_ht40(primary_channel: int, secondary_channel: int):
	"""
	Returns the frequencies of the subcarriers in an HT40 2.4GHz WiFi channel.
	:param primary_channel: The primary channel number.
	:param secondary_channel: The secondary channel number.
	:return: The frequencies of the subcarriers, in Hz, NumPy array.
	"""
	center_primary = constants.WIFI_CHANNEL1_FREQUENCY + constants.WIFI_CHANNEL_SPACING * (primary_channel - 1)
	center_secondary = constants.WIFI_CHANNEL1_FREQUENCY + constants.WIFI_CHANNEL_SPACING * (secondary_channel - 1)
	center_ht40 = (center_primary + center_secondary) / 2
	ht40_subcarrier_count = csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS + csi.HT_COEFFICIENTS_PER_CHANNEL
	assert(ht40_subcarrier_count % 2 == 1)
	return center_ht40 + np.arange(-ht40_subcarrier_count // 2, ht40_subcarrier_count // 2) * constants.WIFI_SUBCARRIER_SPACING

def get_frequencies_lltf(channel: int):
	"""
	Returns the frequencies of the subcarriers in an 2.4GHz 802.11g 20MHz wide WiFi channel.

	:param primary_channel: The primary channel number (= primary channel, but there is only one channel).
	:return: The frequencies of the subcarriers, in Hz, NumPy array.
	"""
	center_lltf = constants.WIFI_CHANNEL1_FREQUENCY + constants.WIFI_CHANNEL_SPACING * (channel - 1)
	lltf_subcarrier_count = csi.LEGACY_COEFFICIENTS_PER_CHANNEL
	return center_lltf + np.arange(-lltf_subcarrier_count // 2, lltf_subcarrier_count // 2) * constants.WIFI_SUBCARRIER_SPACING

def get_cable_wavelength(frequencies: np.ndarray, velocity_factors: np.ndarray):
	"""
	Returns the wavelength of the provided subcarrier frequencies on a cable with the given velocity factors.

	:param frequencies: The frequencies of the subcarriers, in Hz, NumPy array.
	:param velocity_factors: The velocity factors of the cable, NumPy array.
	:return: The wavelengths of the subcarriers, in meters, NumPy array.
	"""
	return constants.SPEED_OF_LIGHT / frequencies[np.newaxis, :] * velocity_factors[:, np.newaxis]

def interpolate_ht40_gap(csi_ht40: np.ndarray):
	"""
	Apply linear interpolation to determine realistic values for the subcarrier channel coefficients in the gap between the bonded channels in an HT40 channel.

	:param csi_ht40: The CSI data for an HT40 channel. Complex-valued NumPy array with arbitrary shape, but the last dimension must be the subcarriers.
	:return: The CSI data with the values in the gap filled in.
	"""
	index_left = csi.HT_COEFFICIENTS_PER_CHANNEL - 1
	index_right = csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS
	missing_indices = np.arange(index_left + 1, index_right)
	left = csi_ht40[..., index_left]
	right = csi_ht40[..., index_right]
	interp = (missing_indices - index_left) / (index_right - index_left)
	csi_ht40[..., missing_indices] = interp * right[..., np.newaxis] + (1 - interp) * left[..., np.newaxis]

def interpolate_lltf_gap(csi_lltf: np.ndarray):
	"""
	Apply linear interpolation to determine realistic values for the subcarrier channel coefficients in the gap between the two halves of the LLTF.

	:param csi_lltf: The CSI data for an LLTF channel. Complex-valued NumPy array with arbitrary shape, but the last dimension must be the subcarriers.
	:return: The CSI data with the values in the gap filled in.
	"""
	index_left = csi_lltf.shape[-1] // 2 - 1
	index_right = csi_lltf.shape[-1] // 2 + 1
	missing_index = csi_lltf.shape[-1] // 2
	csi_lltf[..., missing_index] = (csi_lltf[..., index_left] + csi_lltf[..., index_right]) / 2

def remove_mean_sto(csi_datapoints: np.ndarray):
	"""
	Removes the mean symbol timing offset (STO) from the CSI data by estimating the STO from the phase slope across subcarriers.
	All datapoints are corrected separately.

	:param csi_datapoints: The CSI data (multiple datapoints) to remove the mean STO from, frequency-domain. Complex-valued NumPy array with shape (..., subcarriers).
	"""
	phase_slope = np.angle(np.nansum(csi_datapoints[...,1:] * np.conj(csi_datapoints[...,:-1]), axis = (1, 2, 3, 4)))
	subcarrier_range = np.arange(-csi_datapoints.shape[-1] // 2, csi_datapoints.shape[-1] // 2) + 1
	mean_sto_correction = np.exp(-1.0j * phase_slope[:,np.newaxis] * subcarrier_range[np.newaxis,:])

	csi_datapoints *= mean_sto_correction[:,np.newaxis,np.newaxis,np.newaxis,:]

def shift_to_firstpeak_sync(csi_datapoints: np.ndarray, max_delay_taps = 3, search_resolution = 40, peak_threshold = 0.1):
	"""
	Shifts the CSI data so that the first peak of the channel impulse response is at time 0.
	All CSI datapoints are shifted by the same amount, i.e., requires synchronized CSI.

	:param csi_datapoints: The CSI data to shift, frequency-domain. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
	:param max_delay_taps: The maximum number of time taps to shift the CSI data by.
	:param search_resolution: The number of search points (granularity) to use for the time shift.
	:param peak_threshold: The threshold for the peak detection, as a fraction of the maximum peak power.

	:return: The frequency-domain CSI data with the first peak of the channel impulse response at time 0.
	"""
	# Time-shift all collected CSI so that first "peak" is at time 0
	# CSI datapoints has shape (datapoints, arrays, rows, columns, subcarriers)
	shifts = np.linspace(-max_delay_taps, 0, search_resolution)
	subcarrier_range = np.arange(-csi_datapoints.shape[-1] // 2, csi_datapoints.shape[-1] // 2) + 1
	shift_vectors = np.exp(1.0j * np.outer(shifts, 2 * np.pi * subcarrier_range / csi_datapoints.shape[-1]))
	powers_by_delay = np.sum(np.abs(np.einsum("lbrms,ds->lbrmd", csi_datapoints, shift_vectors))**2, axis = (1, 2, 3))
	max_peaks = np.max(powers_by_delay, axis = -1)
	first_peak = np.argmax(powers_by_delay > peak_threshold * max_peaks[:,np.newaxis], axis = -1)
	shift_to_firstpeak = shift_vectors[first_peak]

	return shift_to_firstpeak[:,np.newaxis,np.newaxis,np.newaxis,:] * csi_datapoints

def fdomain_to_tdomain_pdp_mvdr(csi_fdomain: np.ndarray, chunksize = 36, tap_min = -7, tap_max = 7, resolution = 200):
	"""
	Convert frequency-domain CSI data to a time-domain power delay profile (PDP) using the MVDR beamformer.

	:param: csi_fdomain: The frequency-domain CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
	:return: The delays (in taps) and the PDPs of shape (datapoints, arrays, rows, columns, delays), as NumPy arrays.
	"""
	# Compute the covariance matrix R
	chunksize = csi_fdomain.shape[-1] if chunksize is None else chunksize
	chunkcount = csi_fdomain.shape[-1] // chunksize
	padding = (csi_fdomain.shape[-1] - chunkcount * chunksize) // 2

	csi_chunked = np.reshape(csi_fdomain[..., padding:padding + chunkcount * chunksize], csi_fdomain.shape[:-1] + (chunkcount, chunksize), order = "C")
	R = 1 / csi_chunked.shape[0] * np.einsum("dbrmci,dbrmcj->brmij", csi_chunked, np.conj(csi_chunked))

	delays_taps = np.linspace(tap_min, tap_max, resolution)
	# TODO: get rid of magic constant 128
	steering_vectors = np.exp(-1.0j * 2 * np.pi * np.outer(np.arange(R.shape[-1]), delays_taps / 128))

	R = (R + np.flip(np.conj(R), axis = (3, 4))) / 2
	R = R + 0.1 * np.eye(R.shape[-1])[np.newaxis,np.newaxis,np.newaxis,:,:]

	# Computation using matrix inverse
	#R_inv = np.linalg.inv(R)
	#P_mvdr = 1 / np.real(np.einsum("it,brmij,jt->brmt", np.conj(steering_vectors), R_inv, steering_vectors))

	# Computation using matrix solve
	R_inv_steering_vectors = np.linalg.solve(R, steering_vectors)
	P_mvdr = 1 / np.real(np.einsum("it,brmit->brmt", np.conj(steering_vectors), R_inv_steering_vectors))

	return delays_taps, P_mvdr

def fdomain_to_tdomain_pdp_music(csi_fdomain: np.ndarray, source_count: int = None, chunksize = 36, tap_min = -7, tap_max = 7, resolution = 200):
	"""
	Convert frequency-domain CSI data to a time-domain power delay profile (PDP) using MUSIC super-resolution.

	:param: csi_fdomain: The frequency-domain CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
	:return: The delays (in taps) and the PDPs of shape (datapoints, arrays, rows, columns, delays), as NumPy arrays.
	"""
	# Compute the covariance matrix R
	chunksize = csi_fdomain.shape[-1] if chunksize is None else chunksize
	chunkcount = csi_fdomain.shape[-1] // chunksize
	padding = (csi_fdomain.shape[-1] - chunkcount * chunksize) // 2

	csi_chunked = np.reshape(csi_fdomain[..., padding:padding + chunkcount * chunksize], csi_fdomain.shape[:-1] + (chunkcount, chunksize), order = "C")
	R = 1 / csi_chunked.shape[0] * np.einsum("dbrmci,dbrmcj->brmij", csi_chunked, np.conj(csi_chunked))

	delays_taps = np.linspace(tap_min, tap_max, resolution)
	# TODO: get rid of magic constant 128
	steering_vectors = np.exp(-1.0j * 2 * np.pi * np.outer(np.arange(R.shape[-1]), delays_taps / 128))

	# Use forward–backward correlation matrix (FBCM)
	R = (R + np.flip(np.conj(R), axis = (3, 4))) / 2

	eigval, eigvec = np.linalg.eigh(R)
	eigval = eigval[:,:,:,::-1]
	eigvec = eigvec[:,:,:,:,::-1]

	P_music = np.zeros(R.shape[:3] + (resolution,))
	for array in range(R.shape[0]):
		for row in range(R.shape[1]):
			for col in range(R.shape[2]):
				antenna_source_count = source_count
				if antenna_source_count is None:
					# Rissanen MDL for FBCM, as described in
					# Xinrong Li and Kaveh Pahlavan: "Super-resolution TOA estimation with diversity for indoor geolocation" in IEEE Transactions on Wireless Communications
					ev = np.real(eigval)[array,row,col,:]

					# M = number of chunks for autocorrelation matrix computation, L = maximum number of sources
					M = chunkcount
					L = 10
					mdl = np.zeros(L)

					for k in range(L):
						mdl[k] = -M * (L - k) * (np.sum(np.log(ev[k:L] + 1e-6) / (L - k)) - np.log(np.sum(ev[k:L] + 1e-6) / (L - k)))
						mdl[k] = mdl[k] + (1/4) * k * (2 * L - k + 1) * np.log(M)

					antenna_source_count = np.argmin(mdl)

				Qn = eigvec[array,row,col,:,antenna_source_count:]
				P_music[array,row,col] = 1 / np.linalg.norm(np.einsum("cn,cr->nr", np.conj(Qn), steering_vectors), axis = 0)

	return delays_taps, P_music

def estimate_toas_rootmusic(csi_fdomain: np.ndarray, max_source_count = 2, chunksize = 36, per_board_average = False):
	"""
	Estimate the time of arrivals (ToAs) of the LoS paths using the root-MUSIC algorithm.

	:param csi_fdomain: The frequency-domain CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
	:param max_source_count: The maximum number of sources to estimate. The number of sources is determined using the Rissanen MDL criterion, but this parameter can be used to limit the number of sources.
	:param chunksize: The size of the chunks to use for the covariance matrix computation.
	:param per_board_average: If True, compute the average ToA over all antennas per board. If False, return the ToAs for each antenna separately.
	:return: The estimated ToAs of the LoS paths, in seconds, NumPy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)`.
	"""
	# Compute the covariance matrix R
	chunksize = csi_fdomain.shape[-1] if chunksize is None else chunksize
	chunkcount = csi_fdomain.shape[-1] // chunksize
	padding = (csi_fdomain.shape[-1] - chunkcount * chunksize) // 2

	csi_chunked = np.reshape(csi_fdomain[..., padding:padding + chunkcount * chunksize], csi_fdomain.shape[:-1] + (chunkcount, chunksize), order = "C")

	if per_board_average:
		# Compute R per-board, but add dummy dimensions for row and column
		R = 1 / (csi_chunked.shape[0] * csi_chunked.shape[2] * csi_chunked.shape[3]) * np.einsum("dbrmci,dbrmcj->bij", csi_chunked, np.conj(csi_chunked))
		R = R[:,np.newaxis,np.newaxis,:,:]
	else:
		R = 1 / csi_chunked.shape[0] * np.einsum("dbrmci,dbrmcj->brmij", csi_chunked, np.conj(csi_chunked))

	# Use forward–backward correlation matrix (FBCM)
	R = (R + np.flip(np.conj(R), axis = (3, 4))) / 2

	if chunksize > 50:
		eigval, eigvec = np.linalg.eig(R)
	else:
		eigval, eigvec = np.linalg.eigh(R)

	toas_by_antenna = np.zeros(R.shape[:3])
	for array in range(R.shape[0]):
		for row in range(R.shape[1]):
			for col in range(R.shape[2]):
				# Rissanen MDL for FBCM, as described in
				# Xinrong Li and Kaveh Pahlavan: "Super-resolution TOA estimation with diversity for indoor geolocation" in IEEE Transactions on Wireless Communications
				ev = np.sort(np.real(eigval[array,row,col,:]))[::-1]

				# M = number of chunks for autocorrelation matrix computation, L = maximum number of sources
				M = chunkcount * csi_fdomain.shape[0]
				L = 10
				mdl = np.zeros(L)

				for k in range(L):
					mdl[k] = -M * (L - k) * (np.sum(np.log(ev[k:L] + 1e-6) / (L - k)) - np.log(np.sum(ev[k:L] + 1e-6) / (L - k)))
					mdl[k] = mdl[k] + (1/4) * k * (2 * L - k + 1) * np.log(M)

				antenna_source_count = min(np.argmin(mdl), max_source_count)

				# Now that we determined the number of sources via Rissanen MDL criterion,
				# we can use the root-MUSIC algorithm to estimate the ToAs
				order = np.argsort(np.real(eigval[array,row,col]))[::-1]
				Qn = np.asmatrix(eigvec[array,row,col,:,:][:,order][:,antenna_source_count:])
				C = np.matmul(Qn, Qn.H)

				coeffs = np.asarray([np.trace(C, offset = diag) for diag in range(1, len(C))])

				# Remove some of the smaller noise coefficients, trade accuracy for speed
				coeffs = np.hstack((coeffs[::-1], np.trace(C), coeffs.conj()))

				roots = np.roots(coeffs)
				roots = roots[abs(roots) < 1]
				powers = 1 / (1 - np.abs(roots))
				largest_roots = np.argsort(powers)[::-1]

				source_delays = -np.angle(roots[largest_roots[:antenna_source_count]]) / (2 * np.pi) / constants.WIFI_SUBCARRIER_SPACING
			
				# Out of the strongest 2 paths (or only strongest, if only one source exists), pick the earliest one
				if len(source_delays) > 0:
					toas_by_antenna[array,row,col] = np.min(source_delays[:min(antenna_source_count, 2)])

	# If per-board averaging is enabled, remove dummy dimensions
	if per_board_average:
		toas_by_antenna = toas_by_antenna[:,0,0]

	return toas_by_antenna

def parse_combined_array_config(config_file):
	"""
	Parse the configuration file for demos that use combined array.

	:param config_file: The path to the configuration file.
	:return indexing_matrix: The indexing matrix to map the CSI data of the subarrays to the CSI data of the large array.
	:return board_names_hosts: The names of the boards and their hostnames.
	:return cable_lengths: The lengths of the cables connecting the boards.
	:return cable_velocity_factors: The velocity factors of the cables connecting the boards.
	:return n_rows: The number of rows in the array.
	:return n_cols: The number of columns in the array.
	"""
	with open(config_file, "r") as conffile:
		config = yaml.safe_load(conffile)

	# Make sure array is square
	n_rows = len(config["array"])
	n_cols = len(config["array"][0])
	for row in config["array"]:
		assert(len(row) == n_cols)

	# Build an indexing matrix. The matrix contains the indices to get from the flattened representation
	# of the CSI of all subarrays to the CSI for the large array.
	indexing_matrix = np.zeros((n_rows, n_cols), dtype = int)

	# Collect list of boards and their hosts
	board_names_hosts = dict()
	for boardname in config["boards"].keys():
		board_names_hosts[boardname] = config["boards"][boardname]["host"]

	for row in range(n_rows):
		for col in range(n_cols):
			name, index_row, index_col = config["array"][row][col].split(".")
			offset_board = list(board_names_hosts.keys()).index(name) * constants.ANTENNAS_PER_BOARD
			offset_row = int(index_row) * constants.ANTENNAS_PER_ROW
			indexing_matrix[row, col] = offset_board + offset_row + int(index_col)

	# Get cable lengths and velocity factors
	cable_lengths = np.asarray([board["cable"]["length"] for board in config["boards"].values()])
	cable_velocity_factors = np.asarray([board["cable"]["velocity_factor"] for board in config["boards"].values()])

	cable_lengths = np.asarray([board["cable"]["length"] for board in config["boards"].values()])
	cable_velocity_factors = np.asarray([board["cable"]["velocity_factor"] for board in config["boards"].values()])

	return indexing_matrix, board_names_hosts, cable_lengths, cable_velocity_factors, n_rows, n_cols

def build_combined_array_csi(indexing_matrix, input_csi):
	"""
	Build the combined array CSI data from the CSI data of the subarrays.

	:param indexing_matrix: The indexing matrix to map the CSI data of the subarrays to the CSI data of the large array.
	:param input_csi: The CSI data of the subarrays. Complex-valued NumPy array with shape (datapoints, boards, rows, columns, subcarriers).

	:return: The combined array CSI data. Complex-valued NumPy array with shape (datapoints, rows, columns, subcarriers).
	"""
	# input_csi has shape (datapoint, board, row, column, subcarrier)
	csi_by_array_row_col = np.moveaxis(input_csi, 0, -1)
	csi_ht40_by_antenna = np.reshape(csi_by_array_row_col, (csi_by_array_row_col.shape[0] * csi_by_array_row_col.shape[1] * csi_by_array_row_col.shape[2], csi_by_array_row_col.shape[3], csi_by_array_row_col.shape[4]))
	csi_combined = csi_ht40_by_antenna[indexing_matrix]
	csi_combined = np.moveaxis(csi_combined, -1, 0)

	return csi_combined

def extract_lltf_subcarriers_from_ht40(csi_ht40: np.ndarray, secondary_channel_relative: int):
	"""
	Extract the LLTF subcarriers from HT40 CSI data.

	:param csi_ht40: The HT40 CSI data. Complex-valued NumPy array with shape (..., subcarriers).
	:param secondary_channel_relative: The relative position of the secondary channel to the primary channel. -1 for below, +1 for above.

	:return: The extracted LLTF CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
	"""
	base_offset = (csi.HT_COEFFICIENTS_PER_CHANNEL - csi.LEGACY_COEFFICIENTS_PER_CHANNEL) // 2
	if secondary_channel_relative == -1:
		# Secondary channel is below primary channel
		start_index = base_offset
	else:
		# Secondary channel is above primary channel
		start_index = base_offset + csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS

	return csi_ht40[..., start_index:start_index + csi.LEGACY_COEFFICIENTS_PER_CHANNEL]