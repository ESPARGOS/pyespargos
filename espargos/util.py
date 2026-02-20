#!/usr/bin/env python

import enum
import numpy as np

from . import constants
from . import csi


class AntennaOrientation(enum.Enum):
    """
    Orientation of a sub-array antenna in the combined array.
    Describes how the board-local coordinate system maps to the combined array coordinate system.
    The naming convention uses compass directions: the orientation indicates which direction the
    board's row axis (short axis of the 2x4 sub-array) points in the combined array.
    The N orientation corresponds to the "default" orientation of the board (4 antennas for azimuth, 2 for elevation),
    where the row axis points downwards and the column axis points to the right (typically used when mounted on tripod).
    """

    N = ((1, 0), (0, 1))  # board_row points down (+row), board_col points right (+col): 0° rotation
    E = ((0, 1), (-1, 0))  # board_row points right (+col), board_col points up (-row): 90° CW rotation
    S = ((-1, 0), (0, -1))  # board_row points up (-row), board_col points left (-col): 180° rotation
    W = ((0, -1), (1, 0))  # board_row points left (-col), board_col points down (+row): 270° CW rotation

    def __init__(self, stride_row, stride_col):
        self.stride_row = stride_row
        self.stride_col = stride_col

    def rotation_matrix(self):
        """
        Returns the 2x2 rotation matrix that maps from the board-local coordinate system
        to the combined array coordinate system. This is the same as the stride matrix.
        """
        return np.array(
            [
                [self.stride_row[0], self.stride_col[0]],
                [self.stride_row[1], self.stride_col[1]],
            ],
            dtype=float,
        )


def build_jones_matrices(antenna_orientations: np.ndarray, base_jones_matrix: np.ndarray = None):
    """
    Build per-antenna effective Jones matrices for a combined array, accounting for the physical
    rotation of each sub-array.

    The effective Jones matrix for each antenna maps from the R/L feed basis to the global H/V
    linear polarization basis, taking into account the antenna's physical orientation.

    The physical model: the R/L feed probes are mounted on the antenna and rotate with it,
    while the incoming field has fixed global H/V polarization. Rotating the antenna by angle
    :math:`\\theta` therefore acts in the feed (output) space of the Jones matrix:

    .. math::
        J_{\\text{eff}} = R^T(\\theta) \\cdot J_{\\text{base}}

    Inverting to obtain the R/L → H/V mapping:

    .. math::
        J_{\\text{eff}}^{-1} = J_{\\text{base}}^{-1} \\cdot R(\\theta)

    where :math:`R(\\theta)` is the 2D rotation matrix for the antenna's orientation and
    :math:`J_{\\text{base}}` is the base Jones matrix (H/V to R/L conversion for the default orientation).

    :param antenna_orientations: Array of :class:`AntennaOrientation` values with shape (rows, cols).
    :param base_jones_matrix: The base Jones matrix mapping H/V to R/L for the default (N) orientation.
        If None, uses :data:`constants.ANTENNA_JONES_MATRIX`.

    :return: Array of effective inverse Jones matrices with shape (rows, cols, 2, 2).
        Multiply with R/L feed vector to obtain global H/V: ``H_V = jones[r, c] @ R_L``.
    """
    if base_jones_matrix is None:
        base_jones_matrix = constants.ANTENNA_JONES_MATRIX

    base_jones_inv = np.linalg.inv(base_jones_matrix)

    rows, cols = antenna_orientations.shape
    jones_matrices = np.empty((rows, cols, 2, 2), dtype=base_jones_inv.dtype)

    for r in range(rows):
        for c in range(cols):
            rot = antenna_orientations[r, c].rotation_matrix()
            jones_matrices[r, c] = base_jones_inv @ rot

    return jones_matrices


def csi_interp_iterative(csi: np.ndarray, weights: np.ndarray = None, iterations=10):
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
        weights = np.ones(len(csi), dtype=csi.dtype) / len(csi)

    phi = np.zeros_like(weights, dtype=csi.dtype)
    w = None

    for i in range(iterations):
        w = np.einsum("n,n,n...->...", weights, np.exp(-1.0j * phi), csi)
        phi = np.angle(np.einsum("a,na->n", np.conj(w.flatten()), csi.reshape(len(csi), -1)))
        # err = np.sum([weights[n] * np.linalg.norm(csi[n] - np.exp(1.0j * phi[n]) * w)**2 for n in range(len(csi))])

    return w


def csi_interp_iterative_by_array(csi: np.ndarray, weights: np.ndarray = None, iterations=10):
    """
    Interpolates CSI data (frequency-domain or time-domain) using an iterative algorithm.
    Same as :func:`csi_interp_iterative`, but assumes that second dimension of :code:`csi` is the antenna array dimension and performs the interpolation for each antenna array separately.
    """
    csi_interp = np.zeros((csi.shape[1], *csi.shape[2:]), dtype=csi.dtype)

    for b in range(csi.shape[1]):
        csi_interp[b] = csi_interp_iterative(csi[:, b], weights=weights, iterations=iterations)

    return csi_interp


def csi_interp_eigenvec_per_subcarrier(csi: np.ndarray) -> np.ndarray:
    """
    Interpolates CSI data by finding the principal eigenvector of the per-subcarrier covariance matrix.
    Unlike :func:`csi_interp_eigenvec`, this function computes a separate covariance matrix for each
    subcarrier (last dimension), which preserves the frequency-domain structure of the CSI data.

    The result is scaled by the square root of the principal eigenvalue and phase-referenced to the
    first antenna element (index 0).

    :param csi: Complex-valued CSI data with shape ``(n_samples, *antenna_shape, n_subcarriers)``.
                The first dimension is the number of CSI datapoints (e.g., calibration clusters),
                the last dimension is the number of subcarriers, and any intermediate dimensions
                describe the antenna array geometry.
    :return: Interpolated CSI data with shape ``(*antenna_shape, n_subcarriers)``.
    """
    antenna_shape = csi.shape[1:-1]
    n_subcarriers = csi.shape[-1]

    # Flatten antenna dimensions: (n_samples, n_antennas, n_subcarriers)
    csi_flat = csi.reshape(csi.shape[0], -1, n_subcarriers)

    # Per-subcarrier covariance matrix: (n_subcarriers, n_antennas, n_antennas)
    R = np.einsum("nas,nbs->sab", csi_flat, np.conj(csi_flat))

    # Eigendecomposition, sort by eigenvalue magnitude (descending)
    eigvals, eigvecs = np.linalg.eig(R)
    idx = np.argsort(np.abs(eigvals), axis=1)[:, ::-1]
    eigvals = np.take_along_axis(eigvals, idx, axis=1)
    eigvecs = np.take_along_axis(eigvecs, idx[:, np.newaxis, :], axis=2)

    # Extract principal eigenvector and eigenvalue
    principal_eigenvectors = eigvecs[:, :, 0]
    principal_eigenvalues = eigvals[:, 0]

    # Scale by sqrt of eigenvalue and use antenna 0 as phase reference
    result_flat = np.sqrt(principal_eigenvalues)[:, np.newaxis] * principal_eigenvectors * np.exp(-1.0j * np.angle(principal_eigenvectors[:, 0][:, np.newaxis]))

    # Swap from (n_subcarriers, n_antennas) to (n_antennas, n_subcarriers) and reshape
    result_flat = np.swapaxes(result_flat, 0, 1)
    return result_flat.reshape(antenna_shape + (n_subcarriers,))


def fit_complex_sinusoid(csi_data: np.ndarray) -> np.ndarray:
    """
    Fit a complex sinusoid (amplitude, phase offset, and linear phase slope) to CSI data
    along the subcarrier axis (last dimension).

    Each antenna's frequency response over a reference channel is modeled as:

    .. math::

        H[k] = A \cdot \exp\!\bigl(j\,(\varphi_0 + \omega \, k)\bigr)

    where *k* is the subcarrier index, *A* is the amplitude, :math:`\varphi_0` is the
    phase offset, and :math:`\omega` is the phase slope (proportional to propagation delay).

    The function estimates the parameters per antenna element and returns the
    reconstructed (fitted) complex sinusoid evaluated at every subcarrier index.

    :param csi_data: Complex-valued CSI array with arbitrary leading dimensions
                     (e.g. antenna geometry) and subcarriers as the last dimension.
                     Shape ``(*antenna_shape, n_subcarriers)``.
    :return: Fitted complex sinusoid with the same shape as *csi_data*.
    """
    n_subcarriers = csi_data.shape[-1]
    k = np.arange(n_subcarriers)

    # Estimate phase slope from mean phase increment between adjacent subcarriers
    phase_diff = csi_data[..., 1:] * np.conj(csi_data[..., :-1])
    omega = np.angle(np.sum(phase_diff, axis=-1))  # (*antenna_shape,)

    # Remove phase slope to estimate amplitude and phase offset
    derotated = csi_data * np.exp(-1.0j * omega[..., np.newaxis] * k)
    complex_amplitude = np.mean(derotated, axis=-1)  # A * exp(j * phi_0)

    # Reconstruct fitted sinusoid
    fitted = complex_amplitude[..., np.newaxis] * np.exp(1.0j * omega[..., np.newaxis] * k)
    return fitted


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
    assert ht40_subcarrier_count % 2 == 1
    return center_ht40 + np.arange(-ht40_subcarrier_count // 2, ht40_subcarrier_count // 2) * constants.WIFI_SUBCARRIER_SPACING


def get_frequencies_ht20(channel: int):
    """
    Returns the frequencies of the subcarriers in an 2.4GHz 802.11n 20MHz wide WiFi channel.

    :param primary_channel: The primary channel number (= primary channel, but there is only one channel).
    :return: The frequencies of the subcarriers, in Hz, NumPy array.
    """
    center_ht20 = constants.WIFI_CHANNEL1_FREQUENCY + constants.WIFI_CHANNEL_SPACING * (channel - 1)
    ht20_subcarrier_count = csi.HT_COEFFICIENTS_PER_CHANNEL
    return center_ht20 + np.arange(-ht20_subcarrier_count // 2, ht20_subcarrier_count // 2) * constants.WIFI_SUBCARRIER_SPACING


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


def interpolate_ht40ltf_gap(csi_ht40: np.ndarray):
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


def interpolate_ht20ltf_gap(csi_ht20: np.ndarray):
    """
    Apply linear interpolation to determine a realistic value for the DC subcarrier of the HT20-LTF.

    :param csi_ht20: The CSI data for an HT20 channel. Complex-valued NumPy array with arbitrary shape, but the last dimension must be the subcarriers.
    :return: The CSI data with the values in the gap filled in.
    """
    index_left = csi_ht20.shape[-1] // 2 - 1
    index_right = csi_ht20.shape[-1] // 2 + 1
    missing_index = csi_ht20.shape[-1] // 2
    csi_ht20[..., missing_index] = (csi_ht20[..., index_left] + csi_ht20[..., index_right]) / 2


def interpolate_lltf_gap(csi_lltf: np.ndarray):
    """
    Apply linear interpolation to determine a realistic value for the DC subcarrier of the L-LTF.

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

    :param csi_datapoints: The CSI data (multiple datapoints) to remove the mean STO from, frequency-domain.
                           Complex-valued NumPy array with arbitrary shape as long as the first dimension
                           is the datapoint dimension and the last dimension is the subcarrier dimension.
    """
    # Sum over all axes except the first (datapoints) to get one phase slope per datapoint
    sum_axes = tuple(range(1, csi_datapoints.ndim))
    phase_slope = np.angle(
        np.nansum(
            csi_datapoints[..., 1:] * np.conj(csi_datapoints[..., :-1]),
            axis=sum_axes,
        )
    )
    subcarrier_range = np.arange(-csi_datapoints.shape[-1] // 2, csi_datapoints.shape[-1] // 2) + 1

    # Reshape for broadcasting: (datapoints, 1, 1, ..., 1, subcarriers)
    correction_shape = (csi_datapoints.shape[0],) + (1,) * (csi_datapoints.ndim - 2) + (subcarrier_range.shape[0],)
    mean_sto_correction = np.exp(-1.0j * phase_slope.reshape(-1, 1) * subcarrier_range.reshape(1, -1))

    csi_datapoints *= mean_sto_correction.reshape(correction_shape)


def shift_to_firstpeak_sync(
    csi_datapoints: np.ndarray,
    max_delay_taps=3,
    search_resolution=40,
    peak_threshold=0.1,
):
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
    powers_by_delay = np.sum(
        np.abs(np.einsum("lbrms,ds->lbrmd", csi_datapoints, shift_vectors)) ** 2,
        axis=(1, 2, 3),
    )
    max_peaks = np.max(powers_by_delay, axis=-1)
    first_peak = np.argmax(powers_by_delay > peak_threshold * max_peaks[:, np.newaxis], axis=-1)
    shift_to_firstpeak = shift_vectors[first_peak]

    return shift_to_firstpeak[:, np.newaxis, np.newaxis, np.newaxis, :] * csi_datapoints


def fdomain_to_tdomain_pdp_mvdr(csi_fdomain: np.ndarray, chunksize=36, tap_min=-7, tap_max=7, resolution=200):
    """
    Convert frequency-domain CSI data to a time-domain power delay profile (PDP) using the MVDR beamformer.

    :param: csi_fdomain: The frequency-domain CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
    :return: The delays (in taps) and the PDPs of shape (datapoints, arrays, rows, columns, delays), as NumPy arrays.
    """
    # Compute the covariance matrix R
    chunksize = csi_fdomain.shape[-1] if chunksize is None else chunksize
    chunkcount = csi_fdomain.shape[-1] // chunksize
    padding = (csi_fdomain.shape[-1] - chunkcount * chunksize) // 2

    csi_chunked = np.reshape(
        csi_fdomain[..., padding : padding + chunkcount * chunksize],
        csi_fdomain.shape[:-1] + (chunkcount, chunksize),
        order="C",
    )
    R = 1 / csi_chunked.shape[0] * np.einsum("dbrmci,dbrmcj->brmij", csi_chunked, np.conj(csi_chunked))

    delays_taps = np.linspace(tap_min, tap_max, resolution)
    # TODO: get rid of magic constant 128
    steering_vectors = np.exp(-1.0j * 2 * np.pi * np.outer(np.arange(R.shape[-1]), delays_taps / 128))

    R = (R + np.flip(np.conj(R), axis=(3, 4))) / 2
    R = R + 0.1 * np.eye(R.shape[-1])[np.newaxis, np.newaxis, np.newaxis, :, :]

    # Computation using matrix inverse
    # R_inv = np.linalg.inv(R)
    # P_mvdr = 1 / np.real(np.einsum("it,brmij,jt->brmt", np.conj(steering_vectors), R_inv, steering_vectors))

    # Computation using matrix solve
    R_inv_steering_vectors = np.linalg.solve(R, steering_vectors)
    P_mvdr = 1 / np.real(np.einsum("it,brmit->brmt", np.conj(steering_vectors), R_inv_steering_vectors))

    return delays_taps, P_mvdr


def fdomain_to_tdomain_pdp_music(
    csi_fdomain: np.ndarray,
    source_count: int = None,
    chunksize=36,
    tap_min=-7,
    tap_max=7,
    resolution=200,
):
    """
    Convert frequency-domain CSI data to a time-domain power delay profile (PDP) using MUSIC super-resolution.

    :param: csi_fdomain: The frequency-domain CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
    :return: The delays (in taps) and the PDPs of shape (datapoints, arrays, rows, columns, delays), as NumPy arrays.
    """
    # Compute the covariance matrix R
    chunksize = csi_fdomain.shape[-1] if chunksize is None else chunksize
    chunkcount = csi_fdomain.shape[-1] // chunksize
    padding = (csi_fdomain.shape[-1] - chunkcount * chunksize) // 2

    csi_chunked = np.reshape(
        csi_fdomain[..., padding : padding + chunkcount * chunksize],
        csi_fdomain.shape[:-1] + (chunkcount, chunksize),
        order="C",
    )
    R = 1 / csi_chunked.shape[0] * np.einsum("dbrmci,dbrmcj->brmij", csi_chunked, np.conj(csi_chunked))

    delays_taps = np.linspace(tap_min, tap_max, resolution)
    # TODO: get rid of magic constant 128
    steering_vectors = np.exp(-1.0j * 2 * np.pi * np.outer(np.arange(R.shape[-1]), delays_taps / 128))

    # Use forward–backward correlation matrix (FBCM)
    R = (R + np.flip(np.conj(R), axis=(3, 4))) / 2

    eigval, eigvec = np.linalg.eigh(R)
    eigval = eigval[:, :, :, ::-1]
    eigvec = eigvec[:, :, :, :, ::-1]

    P_music = np.zeros(R.shape[:3] + (resolution,))
    for array in range(R.shape[0]):
        for row in range(R.shape[1]):
            for col in range(R.shape[2]):
                antenna_source_count = source_count
                if antenna_source_count is None:
                    # Rissanen MDL for FBCM, as described in
                    # Xinrong Li and Kaveh Pahlavan: "Super-resolution TOA estimation with diversity for indoor geolocation" in IEEE Transactions on Wireless Communications
                    ev = np.real(eigval)[array, row, col, :]

                    # M = number of chunks for autocorrelation matrix computation, L = maximum number of sources
                    M = chunkcount
                    L = 10
                    mdl = np.zeros(L)

                    for k in range(L):
                        mdl[k] = -M * (L - k) * (np.sum(np.log(ev[k:L] + 1e-6) / (L - k)) - np.log(np.sum(ev[k:L] + 1e-6) / (L - k)))
                        mdl[k] = mdl[k] + (1 / 4) * k * (2 * L - k + 1) * np.log(M)

                    antenna_source_count = np.argmin(mdl)

                Qn = eigvec[array, row, col, :, antenna_source_count:]
                P_music[array, row, col] = 1 / np.linalg.norm(np.einsum("cn,cr->nr", np.conj(Qn), steering_vectors), axis=0)

    return delays_taps, P_music


def estimate_toas_rootmusic(csi_fdomain: np.ndarray, max_source_count=2, chunksize=36, per_board_average=False):
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

    csi_chunked = np.reshape(
        csi_fdomain[..., padding : padding + chunkcount * chunksize],
        csi_fdomain.shape[:-1] + (chunkcount, chunksize),
        order="C",
    )

    if per_board_average:
        # Compute R per-board, but add dummy dimensions for row and column
        R = 1 / (csi_chunked.shape[0] * csi_chunked.shape[2] * csi_chunked.shape[3]) * np.einsum("dbrmci,dbrmcj->bij", csi_chunked, np.conj(csi_chunked))
        R = R[:, np.newaxis, np.newaxis, :, :]
    else:
        R = 1 / csi_chunked.shape[0] * np.einsum("dbrmci,dbrmcj->brmij", csi_chunked, np.conj(csi_chunked))

    # Use forward–backward correlation matrix (FBCM)
    R = (R + np.flip(np.conj(R), axis=(3, 4))) / 2

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
                ev = np.sort(np.real(eigval[array, row, col, :]))[::-1]

                # M = number of chunks for autocorrelation matrix computation, L = maximum number of sources
                M = chunkcount * csi_fdomain.shape[0]
                L = 10
                mdl = np.zeros(L)

                for k in range(L):
                    mdl[k] = -M * (L - k) * (np.sum(np.log(ev[k:L] + 1e-6) / (L - k)) - np.log(np.sum(ev[k:L] + 1e-6) / (L - k)))
                    mdl[k] = mdl[k] + (1 / 4) * k * (2 * L - k + 1) * np.log(M)

                antenna_source_count = min(np.argmin(mdl), max_source_count)

                # Now that we determined the number of sources via Rissanen MDL criterion,
                # we can use the root-MUSIC algorithm to estimate the ToAs
                order = np.argsort(np.real(eigval[array, row, col]))[::-1]
                Qn = np.asmatrix(eigvec[array, row, col, :, :][:, order][:, antenna_source_count:])
                C = np.matmul(Qn, Qn.H)

                coeffs = np.asarray([np.trace(C, offset=diag) for diag in range(1, len(C))])

                # Remove some of the smaller noise coefficients, trade accuracy for speed
                coeffs = np.hstack((coeffs[::-1], np.trace(C), coeffs.conj()))

                roots = np.roots(coeffs)
                roots = roots[abs(roots) < 1]
                powers = 1 / (1 - np.abs(roots))
                largest_roots = np.argsort(powers)[::-1]

                source_delays = -np.angle(roots[largest_roots[:antenna_source_count]]) / (2 * np.pi) / constants.WIFI_SUBCARRIER_SPACING

                # Out of the strongest 2 paths (or only strongest, if only one source exists), pick the earliest one
                if len(source_delays) > 0:
                    toas_by_antenna[array, row, col] = np.min(source_delays[: min(antenna_source_count, 2)])

    # If per-board averaging is enabled, remove dummy dimensions
    if per_board_average:
        toas_by_antenna = toas_by_antenna[:, 0, 0]

    return toas_by_antenna


def parse_combined_array_config(config_dict: dict):
    """
    Parse the configuration file for demos that use combined array.

    :param config_dict: The configuration dictionary to parse.
    :return indexing_matrix: The indexing matrix to map the CSI data of the subarrays to the CSI data of the large array.
    :return board_names_hosts: The names of the boards and their hostnames.
    :return cable_lengths: The lengths of the cables connecting the boards.
    :return cable_velocity_factors: The velocity factors of the cables connecting the boards.
    :return n_rows: The number of rows in the array.
    :return n_cols: The number of columns in the array.
    :return antenna_orientations: Array of :class:`AntennaOrientation` values with shape (n_rows, n_cols), indicating the orientation of each antenna's sub-array in the combined array.
    :raises ValueError: If the configuration is invalid (e.g., invalid antenna indices, missing/duplicate antennas, non-contiguous sub-arrays, invalid rotation).
    """
    config = config_dict.copy()

    # Make sure array is rectangular
    n_rows = len(config["array"])
    if n_rows == 0:
        raise ValueError("Array configuration is empty")
    n_cols = len(config["array"][0])
    if n_cols == 0:
        raise ValueError("Array configuration has empty rows")
    for row_idx, row in enumerate(config["array"]):
        if len(row) != n_cols:
            raise ValueError(f"Array row {row_idx} has {len(row)} columns, expected {n_cols}")

    # Collect list of boards and their hosts
    board_names_hosts = dict()
    for boardname in config["boards"].keys():
        board_names_hosts[boardname] = config["boards"][boardname]["host"]

    # Parse all antenna references and collect per-board mappings:
    # (board_row, board_col) -> (combined_row, combined_col)
    board_antenna_positions = {boardname: dict() for boardname in board_names_hosts.keys()}
    board_orientations = dict()

    for row in range(n_rows):
        for col in range(n_cols):
            entry = config["array"][row][col]
            parts = entry.split(".")
            if len(parts) != 3:
                raise ValueError(f"Invalid antenna reference '{entry}' at row {row}, col {col}. Expected format 'boardname.row.col'")

            name, index_row_str, index_col_str = parts

            if name not in board_names_hosts:
                raise ValueError(f"Unknown board '{name}' referenced at row {row}, col {col}. Available boards: {list(board_names_hosts.keys())}")

            try:
                index_row = int(index_row_str)
                index_col = int(index_col_str)
            except ValueError:
                raise ValueError(f"Non-integer antenna index in reference '{entry}' at row {row}, col {col}")

            if not (0 <= index_row < constants.ROWS_PER_BOARD) or not (0 <= index_col < constants.ANTENNAS_PER_ROW):
                raise ValueError(f"Antenna index out of range in reference '{entry}' at row {row}, col {col}. " f"Expected row in [0, {constants.ROWS_PER_BOARD - 1}], col in [0, {constants.ANTENNAS_PER_ROW - 1}]")

            antenna_id = (index_row, index_col)
            if antenna_id in board_antenna_positions[name]:
                raise ValueError(f"Antenna '{entry}' is used multiple times in the array configuration")
            board_antenna_positions[name][antenna_id] = (row, col)

    # Validate that each board's antennas form a valid, contiguous sub-array:
    # The mapping (board_row, board_col) -> (combined_row, combined_col) must be
    # an affine transformation, i.e., all antennas must be present and adjacent.
    expected_antennas = {(r, c) for r in range(constants.ROWS_PER_BOARD) for c in range(constants.ANTENNAS_PER_ROW)}
    for boardname, positions in board_antenna_positions.items():
        if set(positions.keys()) != expected_antennas:
            missing = expected_antennas - set(positions.keys())
            missing_refs = [f"{boardname}.{r}.{c}" for r, c in sorted(missing)]
            raise ValueError(f"Not all antennas from board '{boardname}' are used. Missing: {missing_refs}")

        # Use antenna (0,0) as origin and compute strides from (1,0) and (0,1)
        origin = np.array(positions[(0, 0)])
        stride_board_row = np.array(positions[(1, 0)]) - origin
        stride_board_col = np.array(positions[(0, 1)]) - origin

        # Match stride vectors to a known orientation (convert to plain int for enum matching)
        stride_tuple = (tuple(int(x) for x in stride_board_row), tuple(int(x) for x in stride_board_col))
        try:
            orientation = AntennaOrientation(stride_tuple)
        except ValueError:
            raise ValueError(f"Board '{boardname}' has an invalid rotation (stride_row={stride_tuple[0]}, stride_col={stride_tuple[1]}). " f"Only 0°/90°/180°/270° rotations are supported, no flips.")

        for (br, bc), (cr, cc) in positions.items():
            expected_pos = origin + br * stride_board_row + bc * stride_board_col
            if not np.array_equal(expected_pos, [cr, cc]):
                raise ValueError(f"Antennas of board '{boardname}' do not form a contiguous sub-array. " f"Antenna {boardname}.{br}.{bc} is at combined array position ({cr}, {cc}), " f"but expected ({expected_pos[0]}, {expected_pos[1]})")

        board_orientations[boardname] = orientation

    # Build the indexing matrix and antenna orientation array from the validated positions
    indexing_matrix = np.zeros((n_rows, n_cols), dtype=int)
    antenna_orientations = np.empty((n_rows, n_cols), dtype=object)
    for boardname, positions in board_antenna_positions.items():
        for (index_row, index_col), (row, col) in positions.items():
            offset_board = list(board_names_hosts.keys()).index(boardname) * constants.ANTENNAS_PER_BOARD
            offset_row = index_row * constants.ANTENNAS_PER_ROW
            indexing_matrix[row, col] = offset_board + offset_row + index_col
            antenna_orientations[row, col] = board_orientations[boardname]

    # Get cable lengths and velocity factors
    cable_lengths = np.asarray([board["cable"]["length"] for board in config["boards"].values()])
    cable_velocity_factors = np.asarray([board["cable"]["velocity_factor"] for board in config["boards"].values()])

    return (
        indexing_matrix,
        board_names_hosts,
        cable_lengths,
        cable_velocity_factors,
        n_rows,
        n_cols,
        antenna_orientations,
    )


def build_combined_array_data(indexing_matrix, input_data):
    """
    Helper for combined array setups. Re-structures data from multiple subarrays into a single large array, using the provided indexing matrix.
    Typically, the input data is the CSI data of the subarrays, but it can also be anything else with shape (datapoints, boards, rows, columns, ...).

    :param indexing_matrix: The indexing matrix to map the CSI data of the subarrays to the CSI data of the large array.
    :param input_data: The data of the subarrays. Complex-valued NumPy array with shape (datapoints, boards, rows, columns, ...).

    :return: The combined array data. Complex-valued NumPy array with shape (datapoints, rows, columns, subcarriers).
    """
    # input_data has shape (datapoint, board, row, column, subcarrier)
    data_by_array_row_col = np.moveaxis(input_data, 0, -1)
    data_by_antenna = np.reshape(
        data_by_array_row_col,
        (data_by_array_row_col.shape[0] * data_by_array_row_col.shape[1] * data_by_array_row_col.shape[2],) + data_by_array_row_col.shape[3:],
    )
    combined_data = data_by_antenna[indexing_matrix]
    combined_data = np.moveaxis(combined_data, -1, 0)

    return combined_data


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

    return csi_ht40[..., start_index : start_index + csi.LEGACY_COEFFICIENTS_PER_CHANNEL]


def extract_ht20_subcarriers_from_ht40(csi_ht40: np.ndarray, secondary_channel_relative: int):
    """
    Extract the HT20 subcarriers from HT40 CSI data.

    :param csi_ht40: The HT40 CSI data. Complex-valued NumPy array with shape (..., subcarriers).
    :param secondary_channel_relative: The relative position of the secondary channel to the primary channel. -1 for below, +1 for above.

    :return: The extracted HT20 CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
    """
    if secondary_channel_relative == -1:
        # Secondary channel is below primary channel
        start_index = 0
    else:
        # Secondary channel is above primary channel
        start_index = csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS

    return csi_ht40[..., start_index : start_index + csi.HT_COEFFICIENTS_PER_CHANNEL]


def extract_lltf_subcarriers_from_ht20(csi_ht20: np.ndarray):
    """
    Extract the LLTF subcarriers from HT20 CSI data.

    :param csi_ht20: The HT20 CSI data. Complex-valued NumPy array with shape (..., subcarriers).

    :return: The extracted LLTF CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
    """
    start_index = (csi.HT_COEFFICIENTS_PER_CHANNEL - csi.LEGACY_COEFFICIENTS_PER_CHANNEL) // 2

    return csi_ht20[..., start_index : start_index + csi.LEGACY_COEFFICIENTS_PER_CHANNEL]


def mask_csi_by_feed(csidata: np.ndarray, rfswitch_states: np.ndarray, desired_feed: csi.rfswitch_state_t):
    """
    Mask the CSI data by the RF switch state, i.e., set the CSI data to 0 for all datapoints where the RF switch state is not the desired feed.
    Also applies scaling to the remaining datapoints to account for the fact that only a fraction of the datapoints are kept, so that the overall power level is preserved.

    :param csidata: The CSI data to mask. Complex-valued NumPy array with shape (datapoints, ..., subcarriers), usually (datapoints, arrays, rows, columns, subcarriers).
    :param rfswitch_states: The RF switch states for each antenna and datapoint. NumPy array with shape (datapoints, ...), usually (datapoints, arrays, rows, columns).
    :param desired_feed: The desired RF switch state to keep.

    :return: The masked CSI data. Complex-valued NumPy array with the same shape as the input CSI data. Returns None if no datapoints have the desired RF switch state for any antenna.
    """
    mask = rfswitch_states == desired_feed
    mask_count = np.sum(mask, axis=0)
    datapoint_count = csidata.shape[0]
    if np.any(mask_count == 0):
        return None
    return csidata * mask[..., np.newaxis] * datapoint_count / mask_count[np.newaxis, ..., np.newaxis]


def separate_feeds(csidata: np.ndarray, rfswitch_state: np.ndarray):
    """
    Separate the CSI data by antenna feeds (R/L) based on the RF switch states.
    Also takes care of scaling the CSI data for each feed to account for the fact that only a fraction of the datapoints are kept for each feed, so that the overall power level is preserved.
    Missing measurements for a feed (i.e., half of all measurements) are filled with zeros.

    :param csidata: The CSI data to separate. Complex-valued NumPy array with shape (datapoints, ..., subcarriers), usually (datapoints, arrays, rows, columns, subcarriers).
    :param rfswitch_states: The RF switch states for each antenna and datapoint. NumPy array with shape (datapoints, ...), usually (datapoints, arrays, rows, columns).

    :return: The separated CSI data. Complex-valued NumPy array with shape (datapoints, ..., subcarriers, 2), where the last dimension corresponds to the R/L feeds. Returns None if no datapoints have the desired RF switch state for any antenna.
    """
    csi_R = mask_csi_by_feed(csidata, rfswitch_state, csi.rfswitch_state_t.SENSOR_RFSWITCH_ANTENNA_R)
    csi_L = mask_csi_by_feed(csidata, rfswitch_state, csi.rfswitch_state_t.SENSOR_RFSWITCH_ANTENNA_L)

    if csi_R is None or csi_L is None:
        return None

    # Separate CSI by feed using element-wise multiplication (zeros where mask is False)
    return np.stack([csi_R, csi_L], axis=-1)  # (D, ..., S, 2), usually (D, B, M, N, S, 2)
