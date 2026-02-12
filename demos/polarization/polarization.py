#!/usr/bin/env python

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin

from espargos.csi import rfswitch_state_t
import espargos.constants
import espargos.util
import numpy as np
import argparse

import PyQt6.QtCore


class EspargosDemoPolarization(BacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    # Signal to update constellation diagrams:
    # (feed_L_points, feed_R_points, linear_H_points, linear_V_points, axis_scale)
    # Each points list contains [[i1, q1], [i2, q2], ...]
    updateConstellation = PyQt6.QtCore.pyqtSignal(list, list, list, list, float)

    # Signal to update polarization ellipse: list of [x, y] points tracing the ellipse, and rotation direction (1=CCW/LHCP, -1=CW/RHCP, 0=linear)
    updatePolarizationEllipse = PyQt6.QtCore.pyqtSignal(list, int)

    DEFAULT_CONFIG = {
        "crosspol_compensation": True,
        "show_mean": True,
    }

    # Smoothed axis scale (exponential moving average)
    _smoothed_axis_scale = None
    _axis_scale_smoothing = 0.1  # Lower = smoother (0.1 means 10% new value, 90% old value)

    def __init__(self, argv):
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Measure dominant polarization of incoming signals",
            add_help=False,
        )
        super().__init__(
            argv,
            argparse_parent=parser,
        )

        # Set up ESPARGOS pool, backlog and QML UI
        self.initialize_pool()

        # Pre-compute per-antenna effective inverse Jones matrices for polarization correction
        # These account for the physical rotation of each sub-array in the combined array
        # Two variants: with full crosspol compensation and with ideal (simple) Jones matrix
        self.jones_matrices_inv_crosspol = espargos.util.build_jones_matrices(self.antenna_orientations)
        self.jones_matrices_inv_simple = espargos.util.build_jones_matrices(self.antenna_orientations, base_jones_matrix=espargos.constants.ANTENNA_JONES_MATRIX_SIMPLE)

        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "polarization-ui.qml")

    @PyQt6.QtCore.pyqtSlot()
    def update(self):
        if (result := self.get_backlog_csi("rssi", "rfswitch_state")) is None:
            return

        csi_backlog, rssi_backlog, rfswitch_state = result

        # Weight CSI data with RSSI (only meaningful when gain is automatic / AGC is enabled)
        if self.pooldrawer.cfgman.get("gain", "automatic"):
            csi_backlog = csi_backlog * 10 ** (rssi_backlog[..., np.newaxis] / 20)

        # Build combined array CSI data
        csi_combined = espargos.util.build_combined_array_data(self.indexing_matrix, csi_backlog)
        csi_combined = csi_combined[:, np.newaxis]  # add fake board dimension: (D, B=1, M, N, S)
        rfswitch_combined = espargos.util.build_combined_array_data(self.indexing_matrix, rfswitch_state)
        rfswitch_combined = rfswitch_combined[:, np.newaxis]

        # Separate CSI by feeds
        csi_by_feed = espargos.util.separate_feeds(csi_combined, rfswitch_combined)  # (D, B=1, M, N, S, 2)

        if csi_by_feed is None:
            print("Must have measurements for both R and L feeds to compute polarization (is RF switch in random mode?)")
            return

        # Apply per-antenna Jones correction to convert R/L feeds to global H/V polarization,
        # accounting for the physical rotation of each sub-array in the combined array.
        # csi_by_feed: (D, B=1, M, N, S, 2) where last dim is R/L
        # jones_matrices_inv: (M, N, 2, 2) maps R/L -> global H/V
        jones_matrices_inv = self.jones_matrices_inv_crosspol if self.appconfig.get("crosspol_compensation") else self.jones_matrices_inv_simple
        csi_by_feed = np.einsum("dbmnsf,mnfp->dbmnsp", csi_by_feed, jones_matrices_inv)

        # Move subcarrier axis to the very front for covariance computation
        csi_by_feed = np.moveaxis(csi_by_feed, -2, 0)  # (S, D, B, M, N, 2)

        # Flatten over all axes (including feeds) except subcarriers and datapoints (the first two)
        csi_by_feed = np.reshape(
            csi_by_feed,
            (csi_by_feed.shape[0], csi_by_feed.shape[1], -1),
        )  # (S, D, B*M*N*2)

        # Compute covariance matrix
        R = np.einsum("sdi,sdj->ij", csi_by_feed, np.conj(csi_by_feed))
        w, v = np.linalg.eig(R)
        dominant_eigenvector = v[:, np.argmax(np.abs(w))]

        # Reshape dominant eigenvector to (B=1, M, N, 2)
        # Data is already in global H/V basis (per-antenna Jones correction applied before covariance)
        dominant_eigenvector = np.reshape(dominant_eigenvector, (1, self.n_rows, self.n_cols, 2))

        # Convert from H/V back to ideal R/L feed basis for feed constellation display
        feed_csi = np.einsum("bmnp,fp->bmnf", dominant_eigenvector, espargos.constants.ANTENNA_JONES_MATRIX_SIMPLE)

        # Get constellation points for L/R feeds
        feed_R_points = feed_csi[..., 0].flatten()
        feed_L_points = feed_csi[..., 1].flatten()

        if self.appconfig.get("show_mean"):
            feed_R_points = np.asarray([np.mean(feed_R_points)])
            feed_L_points = np.asarray([np.mean(feed_L_points)])

        # All phases are relative to the phase of the "stronger" (higher-magnitude) feed for each antenna
        reference_phase = np.angle(np.where(np.abs(feed_R_points) >= np.abs(feed_L_points), feed_R_points, feed_L_points))
        feed_R_points *= np.exp(-1j * reference_phase)
        feed_L_points *= np.exp(-1j * reference_phase)

        # np.set_printoptions(precision=3, suppress=True)
        # mean_jones = np.asarray([np.mean(feed_R_points), np.mean(feed_L_points)])
        # if not hasattr(self, "mean_jones_smooth"):
        #    self.mean_jones_smooth = mean_jones
        # else:
        #    self.mean_jones_smooth = 0.05 * mean_jones + 0.95 * self.mean_jones_smooth
        #    print(self.mean_jones_smooth)

        # Convert complex points to [I, Q] lists for QML
        feed_L_list = [[float(p.real), float(p.imag)] for p in feed_L_points]
        feed_R_list = [[float(p.real), float(p.imag)] for p in feed_R_points]

        # The dominant eigenvector is already in global H/V basis (per-antenna Jones correction
        # was applied before covariance computation), so convert directly
        csi_linear_pol = dominant_eigenvector

        # Determine power ratio between both polarizations
        linear_H_points = csi_linear_pol[..., 0].flatten()
        linear_V_points = csi_linear_pol[..., 1].flatten()

        # Compute mean over points if enabled in config
        if self.appconfig.get("show_mean"):
            linear_H_points = np.asarray([np.mean(linear_H_points)])
            linear_V_points = np.asarray([np.mean(linear_V_points)])

        # All phases are relative to the phase of the "stronger" (higher-magnitude) polarization for each antenna
        reference_phase_linear = np.angle(np.where(np.abs(linear_V_points) >= np.abs(linear_H_points), linear_V_points, linear_H_points))
        linear_H_points *= np.exp(-1j * reference_phase_linear)
        linear_V_points *= np.exp(-1j * reference_phase_linear)

        # Compute unified axis scale to fit all points from both diagrams with some margin
        max_abs_all = max(np.max(np.abs(feed_R_points)), np.max(np.abs(feed_L_points)), np.max(np.abs(linear_H_points)), np.max(np.abs(linear_V_points)))
        target_axis_scale = max_abs_all * 1.2 if max_abs_all > 0 else 1.5

        # Apply exponential moving average smoothing to axis scale
        if self._smoothed_axis_scale is None:
            self._smoothed_axis_scale = target_axis_scale
        else:
            self._smoothed_axis_scale = self._axis_scale_smoothing * target_axis_scale + (1 - self._axis_scale_smoothing) * self._smoothed_axis_scale

        # Convert complex points to [I, Q] lists for QML
        linear_H_list = [[float(p.real), float(p.imag)] for p in linear_H_points]
        linear_V_list = [[float(p.real), float(p.imag)] for p in linear_V_points]

        # Emit constellation data to QML
        self.updateConstellation.emit(feed_L_list, feed_R_list, linear_H_list, linear_V_list, float(self._smoothed_axis_scale))

        # Compute mean H and V components of polarization for displaying polarization as ellipse
        mean_H = np.mean(linear_H_points)
        mean_V = np.mean(linear_V_points)

        # Compute polarization ellipse by sampling the parametric curve:
        # x(t) = Re(mean_H * e^(i*t)), y(t) = Re(mean_V * e^(i*t))
        # This traces out the polarization ellipse as t goes from 0 to 2*pi
        # Negate t to fix the rotation direction (match physical antenna rotation)
        t = np.linspace(0, 2 * np.pi, 100)
        ellipse_x = np.real(mean_H * np.exp(1j * t))
        ellipse_y = np.real(mean_V * np.exp(1j * t))

        # Normalize by the maximum radius of the ellipse (semi-major axis)
        # This ensures the largest dimension always matches the reference circle
        ellipse_radius = np.sqrt(ellipse_x**2 + ellipse_y**2)
        max_radius = np.max(ellipse_radius)
        if max_radius > 0:
            ellipse_x = ellipse_x / max_radius
            ellipse_y = ellipse_y / max_radius

        # Determine rotation direction (handedness) from the phase relationship
        # Im(mean_H * conj(mean_V)) > 0 means V leads H by 90° (LHCP/CCW when looking at source)
        # Im(mean_H * conj(mean_V)) < 0 means H leads V by 90° (RHCP/CW when looking at source)
        cross_product = np.imag(mean_H * np.conj(mean_V))
        threshold = 0.1 * np.abs(mean_H) * np.abs(mean_V)  # Threshold for considering it linear
        if cross_product > threshold:
            rotation_direction = 1  # CCW / LHCP
        elif cross_product < -threshold:
            rotation_direction = -1  # CW / RHCP
        else:
            rotation_direction = 0  # Linear (no rotation)

        # Convert to list of [x, y] points for QML
        ellipse_points = [[float(x), float(y)] for x, y in zip(ellipse_x, ellipse_y)]
        self.updatePolarizationEllipse.emit(ellipse_points, int(rotation_direction))


app = EspargosDemoPolarization(sys.argv)
sys.exit(app.exec())
