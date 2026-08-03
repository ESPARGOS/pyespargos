#!/usr/bin/env python

"""Spatial array processing.

Steering vectors and spatial pseudo-spectra (MUSIC, MVDR) computed from a
caller-provided covariance matrix. Independent of any measurement format.
"""

import numpy as np

__all__ = ["music_spectrum", "mvdr_spectrum", "steering_vectors_2d"]


def steering_vectors_2d(n_rows, n_columns, resolution_azimuth, resolution_elevation):
    """Return a rectangular-array steering grid."""

    phase_columns = np.outer(np.arange(n_columns), np.linspace(-np.pi, np.pi, resolution_azimuth))
    phase_rows = np.outer(np.arange(n_rows), np.linspace(-np.pi, np.pi, resolution_elevation))
    return np.exp(1.0j * (phase_columns[np.newaxis, :, :, np.newaxis] + phase_rows[:, np.newaxis, np.newaxis, :]))


def music_spectrum(covariance, steering_vectors, source_count=1):
    """Compute a MUSIC spectrum for a caller-provided covariance matrix."""

    steering = steering_vectors.reshape(-1, steering_vectors.shape[2], steering_vectors.shape[3])
    covariance = (covariance + np.conj(covariance.T)) / 2
    eigenvalues, eigenvectors = np.linalg.eig(covariance)
    order = np.argsort(eigenvalues)[::-1]
    noise_subspace = eigenvectors[:, order][:, int(source_count) :]
    spectrum = 1 / np.linalg.norm(np.einsum("ae,a...->e...", noise_subspace, np.conj(steering)), axis=0)
    return spectrum - np.min(spectrum) + 1e-6


def mvdr_spectrum(covariance, steering_vectors, diagonal_loading=0.1):
    """Compute an MVDR spectrum for a caller-provided covariance matrix."""

    steering = steering_vectors.reshape(-1, steering_vectors.shape[2], steering_vectors.shape[3])
    covariance = (covariance + np.conj(covariance.T)) / 2
    loading = float(diagonal_loading) * np.trace(covariance) / covariance.shape[0]
    covariance_inv = np.linalg.pinv(covariance + loading * np.eye(covariance.shape[0]))
    denominator = np.einsum("a...,ab,b...->...", np.conj(steering), covariance_inv, steering)
    spectrum = 1.0 / np.maximum(np.real(denominator), 1e-12)
    return spectrum - np.min(spectrum) + 1e-6
