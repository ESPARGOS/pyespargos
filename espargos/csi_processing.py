#!/usr/bin/env python

"""Prepare CSI arrays for analysis.

Operations that know the Wi-Fi preamble formats or the ESPARGOS receiver:
gain compensation, subcarrier-gap interpolation, format extraction and
subcarrier frequency grids, calibration derivation, timing conditioning,
coherent combining of repeated CSI snapshots, and RF-switch feed separation.
"""

import numpy as np

from . import constants
from . import csi_packet
from . import sensor


def scale_csi_by_reported_gain(csi_data: np.ndarray, rx_gain: np.ndarray, fft_gain: np.ndarray) -> np.ndarray:
    """
    Compensate CSI amplitudes for the receiver gain reported by the ESP32.

    The ESP32 reports RX gain in 1 dB units and FFT gain in 0.25 dB units.
    Since these are receiver-side gains, this helper divides raw CSI amplitudes
    by the reported gain factor. The scaling is valid for both automatic and
    manual gain mode, because the reported values are always meaningful.
    """
    rx_gain = _reported_gain_to_signed(rx_gain)
    fft_gain = _reported_gain_to_signed(fft_gain)
    gain_db = constants.RX_GAIN_DB_PER_UNIT * rx_gain + constants.FFT_GAIN_DB_PER_UNIT * fft_gain
    scale = (10.0 ** (-gain_db / 20.0)).astype(np.float32, copy=False)
    return csi_data * scale[..., np.newaxis]


def _reported_gain_to_signed(gain: np.ndarray) -> np.ndarray:
    gain = np.asarray(gain, dtype=np.float32)
    return np.where((gain >= 128.0) & (gain <= 255.0), gain - 256.0, gain)


def interpolate_lltf_gap(csi_lltf: np.ndarray) -> None:
    """
    Fill the L-LTF DC subcarrier by linear interpolation in place.

    :param csi_lltf: Complex L-LTF CSI array. The last dimension must contain
        :data:`espargos.csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL` subcarriers
        in ascending order ``-26..26``. Any leading dimensions are preserved.
    """
    dc_index = csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL // 2
    csi_lltf[..., dc_index] = 0.5 * (csi_lltf[..., dc_index - 1] + csi_lltf[..., dc_index + 1])


def interpolate_ht20ltf_gap(csi_ht20: np.ndarray) -> None:
    """
    Fill the HT20-LTF DC subcarrier by linear interpolation in place.

    :param csi_ht20: Complex HT20-LTF CSI array. The last dimension must contain
        :data:`espargos.csi_packet.HT_COEFFICIENTS_PER_CHANNEL` subcarriers in
        ascending order ``-28..28``. Any leading dimensions are preserved.
    """
    dc_index = csi_packet.HT_COEFFICIENTS_PER_CHANNEL // 2
    csi_ht20[..., dc_index] = 0.5 * (csi_ht20[..., dc_index - 1] + csi_ht20[..., dc_index + 1])


def interpolate_ht40ltf_gap(csi_ht40: np.ndarray) -> None:
    """
    Fill the three HT40 gap subcarriers between primary and secondary channel in place.

    :param csi_ht40: Complex HT40-LTF CSI array. The last dimension must contain
        ``2 * HT_COEFFICIENTS_PER_CHANNEL + HT40_GAP_SUBCARRIERS`` subcarriers
        in ascending order ``-58..58``. Any leading dimensions are preserved.
    """
    index_left = csi_packet.HT_COEFFICIENTS_PER_CHANNEL - 1
    index_right = csi_packet.HT_COEFFICIENTS_PER_CHANNEL + csi_packet.HT40_GAP_SUBCARRIERS
    missing_indices = np.arange(index_left + 1, index_right)
    left = csi_ht40[..., index_left]
    right = csi_ht40[..., index_right]
    interp = (missing_indices - index_left) / (index_right - index_left)
    csi_ht40[..., missing_indices] = interp * right[..., np.newaxis] + (1 - interp) * left[..., np.newaxis]


def interpolate_he20ltf_gaps(csi_he20: np.ndarray) -> None:
    """
    Fill the HE20 invalid subcarriers ``-1, 0, 1`` by linear interpolation in place.

    :param csi_he20: Complex HE20-LTF CSI array. The last dimension must contain
        :data:`espargos.csi_packet.HE20_COEFFICIENTS_PER_CHANNEL` subcarriers in
        ascending order ``-122..122``. Any leading dimensions are preserved.
    """
    center_index = csi_packet.HE20_COEFFICIENTS_PER_CHANNEL // 2
    index_left = center_index - 2
    index_right = center_index + 2
    missing_indices = np.arange(index_left + 1, index_right)
    left = csi_he20[..., index_left]
    right = csi_he20[..., index_right]
    interp = (missing_indices - index_left) / (index_right - index_left)
    csi_he20[..., missing_indices] = interp * right[..., np.newaxis] + (1 - interp) * left[..., np.newaxis]


def extract_lltf_subcarriers_from_ht40(csi_ht40: np.ndarray, secondary_channel_relative: int):
    """
    Extract the LLTF subcarriers from HT40 CSI data.

    :param csi_ht40: The HT40 CSI data. Complex-valued NumPy array with shape (..., subcarriers).
    :param secondary_channel_relative: The relative position of the secondary channel to the primary channel. -1 for below, +1 for above.

    :return: The extracted LLTF CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
    """
    base_offset = (csi_packet.HT_COEFFICIENTS_PER_CHANNEL - csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL) // 2
    if secondary_channel_relative == -1:
        # Secondary channel is below primary channel
        start_index = base_offset
    else:
        # Secondary channel is above primary channel
        start_index = base_offset + csi_packet.HT_COEFFICIENTS_PER_CHANNEL + csi_packet.HT40_GAP_SUBCARRIERS

    return csi_ht40[..., start_index : start_index + csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL]


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
        start_index = csi_packet.HT_COEFFICIENTS_PER_CHANNEL + csi_packet.HT40_GAP_SUBCARRIERS

    return csi_ht40[..., start_index : start_index + csi_packet.HT_COEFFICIENTS_PER_CHANNEL]


def extract_lltf_subcarriers_from_ht20(csi_ht20: np.ndarray):
    """
    Extract the LLTF subcarriers from HT20 CSI data.

    :param csi_ht20: The HT20 CSI data. Complex-valued NumPy array with shape (..., subcarriers).

    :return: The extracted LLTF CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
    """
    start_index = (csi_packet.HT_COEFFICIENTS_PER_CHANNEL - csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL) // 2

    return csi_ht20[..., start_index : start_index + csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL]


def get_frequencies_ht40(primary_channel: int, secondary_channel: int):
    """
    Returns the frequencies of the subcarriers in an HT40 2.4GHz WiFi channel.
    :param primary_channel: The primary channel number.
    :param secondary_channel: The secondary channel number.
    :return: The frequencies of the subcarriers, in Hz, NumPy array.
    """
    center_ht40 = get_center_frequency(primary_channel, secondary_channel)
    return center_ht40 + csi_packet.get_csi_format_subcarrier_indices("ht40") * constants.WIFI_SUBCARRIER_SPACING


def get_center_frequency(primary_channel: int, secondary_channel: int | None = None):
    """
    Returns the RF center frequency for the provided Wi-Fi channel configuration.

    If only ``primary_channel`` is given, this returns the center frequency of that
    20 MHz channel. If ``secondary_channel`` is also given, this returns the center
    frequency halfway between primary and secondary, which corresponds to the HT40 LO.

    :param primary_channel: The primary Wi-Fi channel number.
    :param secondary_channel: The secondary Wi-Fi channel number. If omitted or equal
        to ``primary_channel``, the 20 MHz channel center is returned.
    :return: Center frequency in Hz.
    """
    center_primary = constants.WIFI_CHANNEL1_FREQUENCY + constants.WIFI_CHANNEL_SPACING * (primary_channel - 1)
    if secondary_channel is None or secondary_channel == primary_channel:
        return center_primary

    center_secondary = constants.WIFI_CHANNEL1_FREQUENCY + constants.WIFI_CHANNEL_SPACING * (secondary_channel - 1)
    return (center_primary + center_secondary) / 2


def get_frequencies_ht20(channel: int):
    """
    Returns the frequencies of the subcarriers in an 2.4GHz 802.11n 20MHz wide WiFi channel.

    :param primary_channel: The primary channel number (= primary channel, but there is only one channel).
    :return: The frequencies of the subcarriers, in Hz, NumPy array.
    """
    center_ht20 = get_center_frequency(channel)
    return center_ht20 + csi_packet.get_csi_format_subcarrier_indices("ht20") * constants.WIFI_SUBCARRIER_SPACING


def get_frequencies_he20(channel: int):
    """
    Returns the frequencies of the subcarriers in a 2.4 GHz 802.11ax HE20 channel.

    The raw HE-LTF reported by the ESP32-C61 covers subcarrier indices ``-122..122``,
    where ``-1, 0, 1`` are invalid / null tones.

    :param channel: The primary channel number.
    :return: The frequencies of the HE20 subcarriers, in Hz, NumPy array.
    """
    center_he20 = get_center_frequency(channel)
    return center_he20 + csi_packet.get_csi_format_subcarrier_indices("he20").astype(np.float64) * (constants.WIFI_SUBCARRIER_SPACING / 4.0)


def get_frequencies_lltf(channel: int):
    """
    Returns the frequencies of the subcarriers in an 2.4GHz 802.11g 20MHz wide WiFi channel.

    :param primary_channel: The primary channel number (= primary channel, but there is only one channel).
    :return: The frequencies of the subcarriers, in Hz, NumPy array.
    """
    center_lltf = get_center_frequency(channel)
    return center_lltf + csi_packet.get_csi_format_subcarrier_indices("lltf") * constants.WIFI_SUBCARRIER_SPACING


def _wrap_period_symmetric(values: np.ndarray, period: float) -> np.ndarray:
    """
    Wrap values into the interval ``[-period / 2, period / 2)``.
    """
    return np.mod(values + period / 2.0, period) - period / 2.0


def derive_he20_calibration_from_lltf(
    complete_clusters_lltf: np.ndarray,
    complete_cluster_timestamps: np.ndarray,
    secondary_channel_relative: int,
) -> np.ndarray:
    """
    Derive a phase calibration for HE20 CSI from calibration packets that only
    provide LLTF.

    HE20 uses four times finer subcarrier spacing than LLTF / HT20. This means
    that a delay which is only observed on the coarse 312.5 kHz LLTF / HT20
    grid is ambiguous when projected onto the denser 78.125 kHz HE20 grid:
    multiple HE20 phase slopes can agree on every fourth subcarrier while
    disagreeing on the intermediate HE20 tones. We therefore cannot obtain a
    reliable HE20 calibration by simply fitting a slope on the coarse grid and
    reusing it unchanged.

    This helper resolves the problem by going back to "first principles" of
    calibration and estimating time and phase offset separately:

    1. Estimate constant per-antenna phase offsets from the already
       STO-corrected LLTF calibration clusters using a principal-eigenvector
       estimate.
    2. Undo the LLTF timestamp-based STO correction, recover the underlying
       per-antenna baseband timing offsets from the raw LLTF slope together with
       the calibration timestamps, and synthesize the corresponding HE20 phase
       slope on the denser HE20 subcarrier grid.

    The final HE20 calibration is the combination of those per-antenna constant
    phase offsets and the timestamp-derived HE20 phase slope.

    :param complete_clusters_lltf: Complete LLTF calibration CSI clusters as a
        complex-valued NumPy array with shape
        ``(clusters, boards, rows, columns, subcarriers)``. These values are
        expected to come from :meth:`CSICluster.deserialize_csi_lltf` and are
        therefore already STO-corrected using the forwarded hardware
        timestamps.
    :param complete_cluster_timestamps: Per-sensor timestamps corresponding to
        ``complete_clusters_lltf``, in seconds, as a NumPy array with shape
        ``(clusters, boards, rows, columns)``.
    :param secondary_channel_relative: Relative position of the secondary
        channel used for the calibration packets. Use ``-1`` for HT40 below,
        ``+1`` for HT40 above, and ``0`` for a plain 20 MHz channel.
    :return: Complex-valued HE20 calibration array with shape
        ``(boards, rows, columns, csi_packet.HE20_COEFFICIENTS_PER_CHANNEL)``.
    """
    # First estimate per-antenna constant phase offsets from the LLTF
    # calibration clusters exactly as provided by deserialize_csi_lltf(), i.e.
    # after its timestamp-based STO correction. Use a principal-eigenvector
    # estimate so that we combine all clusters and subcarriers coherently.
    csi_lltf_sto_corrected = np.asarray(complete_clusters_lltf, dtype=np.complex64)

    # Undo the timestamp-based STO correction from deserialize_csi_lltf().
    subcarrier_range = csi_packet.get_csi_format_subcarrier_indices("lltf").astype(np.float64)[np.newaxis, np.newaxis, np.newaxis, np.newaxis, :]
    subcarrier_range -= secondary_channel_relative * int(2 * constants.WIFI_CHANNEL_SPACING / constants.WIFI_SUBCARRIER_SPACING)
    sto_delay_correction = np.exp(1.0j * 2 * np.pi * complete_cluster_timestamps[:, :, :, :, np.newaxis] * constants.WIFI_SUBCARRIER_SPACING * subcarrier_range)

    csi_lltf = np.einsum("cbras,cbras->cbras", csi_lltf_sto_corrected, sto_delay_correction)

    csi_lltf_flat = np.moveaxis(csi_lltf, -1, 1).reshape(csi_lltf.shape[0] * csi_lltf.shape[-1], -1)
    covariance = np.einsum("na,nb->ab", csi_lltf_flat, np.conj(csi_lltf_flat)) / max(csi_lltf_flat.shape[0], 1)
    eigvals, eigvecs = np.linalg.eig(covariance)
    principal_eigenvector = eigvecs[:, np.argmax(np.real(eigvals))].reshape(csi_lltf.shape[1:4])
    principal_eigenvector /= principal_eigenvector[0, 0, 0] / np.abs(principal_eigenvector[0, 0, 0])
    antenna_phase_offsets = principal_eigenvector / np.abs(principal_eigenvector)

    # Now we have the "raw" CSI and timestamps from the hardware again.
    # First, determine the STO from the csi_lltf slope
    incr = csi_lltf[..., 1:] * np.conj(csi_lltf[..., :-1])
    sto = np.angle(np.sum(incr, axis=-1)) / (2.0 * np.pi * constants.WIFI_SUBCARRIER_SPACING)  # in seconds

    # Now we can compute absolute timing for each cluster
    packet_times = complete_cluster_timestamps - sto

    rx_baseband_sto = packet_times[:, :, :, :] - packet_times[:, 0:1, 0:1, 0:1]

    mean_rx_baseband_sto = np.mean(rx_baseband_sto, axis=0)
    he20_subcarrier_indices = csi_packet.get_csi_format_subcarrier_indices("he20").astype(np.float64)
    he20_frequencies_hz = he20_subcarrier_indices * (constants.WIFI_SUBCARRIER_SPACING / 4.0)
    calibration_he20 = np.exp(-1.0j * 2.0 * np.pi * mean_rx_baseband_sto[..., np.newaxis] * he20_frequencies_hz[np.newaxis, np.newaxis, np.newaxis, :]).astype(np.complex64)
    calibration_he20 *= antenna_phase_offsets[..., np.newaxis].astype(np.complex64)

    return calibration_he20


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


def csi_interp_iterative(csi: np.ndarray, weights: np.ndarray = None, iterations=10):
    """
    Coherently combines repeated CSI observations by iteratively phase-aligning them.

    Each CSI snapshot is assumed to differ from the others mainly by a single
    global phase rotation. The algorithm alternates between two steps:
    estimating a combined CSI from the current phase offsets, and updating the
    phase offset of each snapshot to best match that combined CSI.

    :param csi: The CSI data to interpolate. Complex-valued NumPy array. Can be an array with arbitrary dimensions, but the first dimension must be the number of CSI datapoints.
    :param weights: The weights to use for each CSI datapoint. If None, all datapoints are weighted equally.
    :param iterations: The number of iterations to perform. Default is 10.

    :return: A phase-aligned weighted average of the input CSI data, with the
             same shape as one CSI datapoint.
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


def fit_complex_sinusoid(csi_data: np.ndarray) -> np.ndarray:
    r"""
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


def mask_csi_by_feed(csidata: np.ndarray, rfswitch_states: np.ndarray, desired_feed: sensor.RFSwitchState):
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
    csi_R = mask_csi_by_feed(csidata, rfswitch_state, sensor.RFSwitchState.SENSOR_RFSWITCH_ANTENNA_R)
    csi_L = mask_csi_by_feed(csidata, rfswitch_state, sensor.RFSwitchState.SENSOR_RFSWITCH_ANTENNA_L)

    if csi_R is None or csi_L is None:
        return None

    # Separate CSI by feed using element-wise multiplication (zeros where mask is False)
    return np.stack([csi_R, csi_L], axis=-1)  # (D, ..., S, 2), usually (D, B, M, N, S, 2)
