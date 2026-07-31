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


def get_csi_sto_correction_frequencies(csi_format: str, secondary_channel_relative: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """
    Return the baseband frequency grid on which CSI of a format is STO-corrected.

    :meth:`CSICluster.deserialize_csi_*` applies its timestamp-based STO
    correction on a format-specific baseband subcarrier grid. Any code that
    estimates or synthesizes per-antenna phase slopes on deserialized CSI (in
    particular :func:`estimate_phase_time_offsets`) must use the exact same
    grid. For L-LTF and HT20 the grid is referenced to the receiver LO center
    (the HT40 mid-frequency in 40 MHz mode) and HT40 is natively LO-centered.
    HE20 uses a primary-channel-centered grid; since HE20 cannot be received
    with channel bonding, that always coincides with the LO center.

    :param csi_format: One of ``"lltf"``, ``"ht20"``, ``"ht40"``, ``"he20"``.
    :param secondary_channel_relative: Relative position of the secondary
        channel: ``-1`` for HT40 below, ``+1`` for HT40 above, ``0`` for none.
    :return: Tuple of baseband frequencies in Hz (one per subcarrier) and a
        boolean validity mask (False for gap / invalid subcarriers that carry
        no usable CSI, e.g. the HT40 gap).
    """
    indices = csi_packet.get_csi_format_subcarrier_indices(csi_format).astype(np.float64)
    valid = np.ones(len(indices), dtype=bool)

    if csi_format in ("lltf", "ht20"):
        indices = indices - secondary_channel_relative * int(2 * constants.WIFI_CHANNEL_SPACING / constants.WIFI_SUBCARRIER_SPACING)
        frequencies = indices * constants.WIFI_SUBCARRIER_SPACING
    elif csi_format == "ht40":
        frequencies = indices * constants.WIFI_SUBCARRIER_SPACING
        gap_start = csi_packet.HT_COEFFICIENTS_PER_CHANNEL
        valid[gap_start : gap_start + csi_packet.HT40_GAP_SUBCARRIERS] = False
    elif csi_format == "he20":
        if secondary_channel_relative != 0:
            raise ValueError("HE20 CSI is not available with channel bonding")
        frequencies = indices * (constants.WIFI_SUBCARRIER_SPACING / 4.0)
        valid[np.isin(indices, (-1.0, 0.0, 1.0))] = False
    else:
        raise ValueError(f"Unknown CSI format {csi_format!r}")

    return frequencies, valid


def estimate_phase_time_offsets(
    complete_clusters: np.ndarray,
    complete_cluster_timestamps: np.ndarray,
    subcarrier_frequencies: np.ndarray,
    valid_subcarriers: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Estimate per-antenna timing and phase offsets from calibration CSI clusters.

    This is the "first principles" view of calibration: after the receivers
    have locked, each antenna differs from the reference antenna only by a
    baseband timing offset and a constant phase offset. Both are estimated
    separately:

    1. Undo the timestamp-based STO correction that deserialization applied,
       recovering the raw per-antenna responses, and determine the STO of each
       cluster from their phase slopes across the subcarrier grid.
    2. Anchor the slopes to the hardware timestamps: the per-antenna timing
       offsets are the mean differences of the recovered packet times relative
       to the reference antenna. This makes them absolute, so they stay
       unambiguous when projected onto denser subcarrier grids (e.g. HE20)
       than the one they were measured on.
    3. Estimate the constant per-antenna phase offsets from the STO-corrected
       responses (which are stable over clusters), after removing the phase
       slope that the timing offsets explain. The timestamp epochs contained
       in the timing offsets make the raw responses rotate rapidly across
       subcarriers, so the phase must be estimated in this slope-free domain.

    The function is format-agnostic: it accepts deserialized clusters of any
    CSI format together with that format's STO-correction grid, obtained from
    :func:`get_csi_sto_correction_frequencies`. Estimating from the widest
    available band yields the most accurate timing offsets.

    :param complete_clusters: Complete calibration CSI clusters as a
        complex-valued NumPy array with shape
        ``(clusters, boards, rows, columns, subcarriers)``. These values are
        expected to come from :meth:`CSICluster.deserialize_csi_*` and are
        therefore already STO-corrected using the forwarded hardware
        timestamps.
    :param complete_cluster_timestamps: Per-sensor timestamps corresponding to
        ``complete_clusters``, in seconds, as a NumPy array with shape
        ``(clusters, boards, rows, columns)``.
    :param subcarrier_frequencies: The baseband frequency grid the clusters
        were STO-corrected on, in Hz, one entry per subcarrier. Must be
        uniformly spaced across the valid subcarriers.
    :param valid_subcarriers: Optional boolean mask marking subcarriers that
        carry usable CSI. Gap / invalid subcarriers are excluded from the
        estimation. If None, all subcarriers are used.
    :return: Tuple of per-antenna timing offsets (in seconds, real-valued) and
        phase offsets (as complex values on the unit circle), both of shape
        ``(boards, rows, columns)``, relative to antenna ``(0, 0, 0)``.
    """
    csi_sto_corrected = np.asarray(complete_clusters, dtype=np.complex64)
    frequencies = np.asarray(subcarrier_frequencies, dtype=np.float64)
    valid = np.ones(len(frequencies), dtype=bool) if valid_subcarriers is None else np.asarray(valid_subcarriers, dtype=bool)

    pair_valid = valid[:-1] & valid[1:]
    spacings = np.diff(frequencies)[pair_valid]
    spacing = float(np.mean(spacings))
    if not np.allclose(spacings, spacing, rtol=1e-6, atol=1e-3):
        raise ValueError("subcarrier_frequencies must be uniformly spaced across valid subcarriers")

    # Undo the timestamp-based STO correction from deserialization
    sto_delay_correction = np.exp(1.0j * 2 * np.pi * complete_cluster_timestamps[:, :, :, :, np.newaxis] * frequencies)
    csi_raw = np.einsum("cbras,cbras->cbras", csi_sto_corrected, sto_delay_correction)

    # Determine the STO of each cluster from the raw phase slope across the grid
    incr = (csi_raw[..., 1:] * np.conj(csi_raw[..., :-1]))[..., pair_valid]
    sto = np.angle(np.sum(incr, axis=-1)) / (2.0 * np.pi * spacing)  # in seconds

    # Now we can compute absolute timing for each cluster; anchoring the slope to
    # the hardware timestamps makes the per-antenna timing offsets absolute
    packet_times = complete_cluster_timestamps - sto

    rx_baseband_sto = packet_times[:, :, :, :] - packet_times[:, 0:1, 0:1, 0:1]
    mean_rx_baseband_sto = np.mean(rx_baseband_sto, axis=0)

    # Constant per-antenna phase offsets in the slope-free domain
    mean_response = csi_interp_eigenvec_per_subcarrier(csi_sto_corrected[..., valid])
    timing_slope = np.exp(-1.0j * 2.0 * np.pi * mean_rx_baseband_sto[..., np.newaxis] * frequencies[valid])
    residual = mean_response * np.conj(timing_slope)
    residual_sum = np.sum(residual, axis=-1)
    antenna_phase_offsets = residual_sum / np.maximum(np.abs(residual_sum), 1e-12)
    antenna_phase_offsets = antenna_phase_offsets * np.conj(antenna_phase_offsets[0:1, 0:1, 0:1] / np.abs(antenna_phase_offsets[0:1, 0:1, 0:1]))

    return mean_rx_baseband_sto, antenna_phase_offsets


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
