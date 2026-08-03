#!/usr/bin/env python

"""Super-resolution delay-domain estimation from frequency-domain channel measurements.

Converts frequency-domain CSI into power delay profiles (MVDR / MUSIC
variants) and estimates times of arrival (root-MUSIC). All estimators share
the same chunked-covariance preprocessing over the subcarrier axis.
"""

import numpy as np

__all__ = ["estimate_toas_rootmusic", "fdomain_to_tdomain_pdp_music", "fdomain_to_tdomain_pdp_mvdr"]

from . import constants


def _chunk_subcarriers(csi_fdomain: np.ndarray, chunksize):
    """Split the subcarrier axis into equal chunks for covariance estimation.

    Returns the chunked CSI with shape ``(..., chunks, chunksize)`` and the
    number of chunks. Subcarriers that do not fit are trimmed symmetrically.
    """
    chunksize = csi_fdomain.shape[-1] if chunksize is None else chunksize
    chunkcount = csi_fdomain.shape[-1] // chunksize
    padding = (csi_fdomain.shape[-1] - chunkcount * chunksize) // 2

    csi_chunked = np.reshape(
        csi_fdomain[..., padding : padding + chunkcount * chunksize],
        csi_fdomain.shape[:-1] + (chunkcount, chunksize),
        order="C",
    )
    return csi_chunked, chunkcount


def fdomain_to_tdomain_pdp_mvdr(csi_fdomain: np.ndarray, chunksize=36, tap_min=-7, tap_max=7, resolution=200):
    """
    Convert frequency-domain CSI data to a time-domain power delay profile (PDP) using the MVDR beamformer.

    .. warning:: The chunking default and the tap steering vectors are tuned
        for the coarse 312.5 kHz subcarrier grids (LLTF / HT20 / HT40). HE20
        input, whose subcarriers are spaced four times finer, needs review.

    :param: csi_fdomain: The frequency-domain CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
    :return: The delays (in taps) and the PDPs of shape (datapoints, arrays, rows, columns, delays), as NumPy arrays.
    """
    # Compute the covariance matrix R
    csi_chunked, chunkcount = _chunk_subcarriers(csi_fdomain, chunksize)
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

    .. warning:: The chunking default and the tap steering vectors are tuned
        for the coarse 312.5 kHz subcarrier grids (LLTF / HT20 / HT40). HE20
        input, whose subcarriers are spaced four times finer, needs review.

    :param: csi_fdomain: The frequency-domain CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
    :return: The delays (in taps) and the PDPs of shape (datapoints, arrays, rows, columns, delays), as NumPy arrays.
    """
    # Compute the covariance matrix R
    csi_chunked, chunkcount = _chunk_subcarriers(csi_fdomain, chunksize)
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

    .. warning:: The phase-to-delay conversion assumes the standard 312.5 kHz
        Wi-Fi subcarrier spacing. HE20 input, whose subcarriers are spaced
        four times finer, would yield ToAs off by a factor of four and needs
        review.

    :param csi_fdomain: The frequency-domain CSI data. Complex-valued NumPy array with shape (datapoints, arrays, rows, columns, subcarriers).
    :param max_source_count: The maximum number of sources to estimate. The number of sources is determined using the Rissanen MDL criterion, but this parameter can be used to limit the number of sources.
    :param chunksize: The size of the chunks to use for the covariance matrix computation.
    :param per_board_average: If True, compute the average ToA over all antennas per board. If False, return the ToAs for each antenna separately.
    :return: The estimated ToAs of the LoS paths, in seconds, NumPy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)`.
    """
    # Compute the covariance matrix R
    csi_chunked, chunkcount = _chunk_subcarriers(csi_fdomain, chunksize)

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
