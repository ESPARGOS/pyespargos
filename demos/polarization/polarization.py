#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, SingleCSIFormatMixin

from espargos.csi import rfswitch_state_t
import espargos.constants
import numpy as np
import argparse

import PyQt6.QtCore


class EspargosDemoPolarization(BacklogMixin, SingleCSIFormatMixin, ESPARGOSApplication):
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
        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "polarization-ui.qml")

    @PyQt6.QtCore.pyqtSlot()
    def update(self):
        if (result := self.get_backlog_csi("rssi", "rfswitch_state")) is None:
            return

        csi_backlog, rssi_backlog, rfswitch_state = result

        # Scale CSI with RSSI to account for different signal strengths
        # csi_backlog = csi_backlog * 10 ** (rssi_backlog[..., np.newaxis] / 20)

        # The antenna has two feeds (RF switch state can vary per antenna):
        # - rfswitch_state_t.SENSOR_RFSWITCH_ANTENNA_R
        # - rfswitch_state_t.SENSOR_RFSWITCH_ANTENNA_L

        # Create masks and expand to include subcarrier dimension for broadcasting
        # rfswitch_state: (D, B, M, N), csi_backlog: (D, B, M, N, S)
        mask_R = rfswitch_state == rfswitch_state_t.SENSOR_RFSWITCH_ANTENNA_R  # (D, B, M, N)
        mask_L = rfswitch_state == rfswitch_state_t.SENSOR_RFSWITCH_ANTENNA_L  # (D, B, M, N)

        mask_R_count = np.sum(mask_R, axis=0)
        mask_L_count = np.sum(mask_L, axis=0)

        if np.any(mask_R_count == 0) or np.any(mask_L_count == 0):
            print("Need to receive both R and L feeds to compute polarization")
            return

        csi_R = csi_backlog * mask_R[..., np.newaxis]
        csi_L = csi_backlog * mask_L[..., np.newaxis]

        # Scale CSI to account for different number of samples per feed
        csi_R *= csi_backlog.shape[0] / mask_R_count[..., np.newaxis]
        csi_L *= csi_backlog.shape[0] / mask_L_count[..., np.newaxis]

        # Separate CSI by feed using element-wise multiplication (zeros where mask is False)
        csi_by_feed = np.stack([csi_R, csi_L], axis=-1)  # (D, B, M, N, S, 2)

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

        # Reshape dominant eigenvector to (B, M, N, 2)
        dominant_eigenvector = np.reshape(dominant_eigenvector, csi_backlog.shape[1:-1] + (2,))

        if self.appconfig.get("crosspol_compensation"):
            feed_csi = np.einsum("bmnf,fp->bmnp", dominant_eigenvector, np.linalg.inv(espargos.constants.ANTENNA_JONES_CROSSPOL_MATRIX))
        else:
            feed_csi = dominant_eigenvector

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

        # Use antenna Jones matrix to convert from feed (R/L) to linear (H/V) polarization basis
        jones_matrix = espargos.constants.ANTENNA_JONES_MATRIX if self.appconfig.get("crosspol_compensation") else espargos.constants.ANTENNA_JONES_MATRIX_SIMPLE
        csi_linear_pol = np.einsum("bmnf,fp->bmnp", dominant_eigenvector, np.linalg.inv(jones_matrix))

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
