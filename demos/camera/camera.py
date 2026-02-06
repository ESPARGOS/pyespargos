#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin

import matplotlib.colors
import numpy as np
import espargos
import argparse
import time

import PyQt6.QtMultimedia
import PyQt6.QtCore

import videocamera


class EspargosDemoCamera(BacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    rssiChanged = PyQt6.QtCore.pyqtSignal(float)
    activeAntennasChanged = PyQt6.QtCore.pyqtSignal(float)
    beamspacePowerImagedataChanged = PyQt6.QtCore.pyqtSignal(list)
    polarizationImagedataChanged = PyQt6.QtCore.pyqtSignal(list)
    recentMacsChanged = PyQt6.QtCore.pyqtSignal(list)
    cameraFlipChanged = PyQt6.QtCore.pyqtSignal()
    rawBeamspaceChanged = PyQt6.QtCore.pyqtSignal()
    fovAzimuthChanged = PyQt6.QtCore.pyqtSignal()
    fovElevationChanged = PyQt6.QtCore.pyqtSignal()
    resolutionAzimuthChanged = PyQt6.QtCore.pyqtSignal()
    resolutionElevationChanged = PyQt6.QtCore.pyqtSignal()
    macListEnabledChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "receiver": {"mac_list_enabled": False},
        "camera": {
            "enable": True,
            "flip": False,
            "format": None,  # will be populated by app, can take values like "1920x1080 @ 30.00 FPS",
            "device": None,  # will be populated by app, can take values like "/dev/video0"
            "fov_azimuth": 72,
            "fov_elevation": 41,
        },
        "beamformer": {
            "type": "FFT",
            "colorize_delay": False,
            "polarization_mode": "show",
            "max_delay": 0.2,
            "max_age": 0.0,
            "resolution_azimuth": 64,
            "resolution_elevation": 32,
        },
        "visualization": {
            "space": "Camera",
            "overlay": "Default",
            "manual_exposure": False,
            "exposure": 0.5,
        },
    }

    def __init__(self, argv):
        # Parse command line arguments
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Overlay received power on top of camera image",
            add_help=False,
        )
        parser.add_argument(
            "-a",
            "--additional-calibration",
            type=str,
            default="",
            help="File to read additional phase calibration results from",
        )
        parser.add_argument(
            "--csi-completion-timeout",
            type=float,
            default=0.2,
            help="Time after which CSI cluster is considered complete even if not all antennas have provided data. Set to zero to disable processing incomplete clusters.",
        )
        parser.add_argument(
            "--no-camera",
            default=False,
            help="Do not actually use camera, only show spatial spectrum visualization",
            action="store_true",
        )
        super().__init__(
            argv,
            argparse_parent=parser,
        )

        # Load additional calibration data from file, if provided
        self.additional_calibration = None
        if len(self.args.additional_calibration) > 0:
            self.additional_calibration = np.load(self.args.additional_calibration)

        # Initialize combined array setup
        self.initialize_pool(backlog_cb_predicate=self._cb_predicate)

        # Parse no-camera command line argument, overrides initial app config
        if self.args.no_camera:
            self.appconfig.set({"camera": {"enable": False}})

        # Camera setup (if enabled in config)
        if self.appconfig.get("camera", "enable"):
            self.videocamera = videocamera.VideoCamera(
                self.appconfig.get("camera", "device"),
                self.appconfig.get("camera", "format"),
            )

            # Let UI know about currently selected camera device and format
            self.appconfig.set(
                {
                    "camera": {
                        "device": self.videocamera.getDevice(),
                        "format": self.videocamera.getFormat(),
                    }
                }
            )
        else:
            self.videocamera = videocamera.DummyVideoCamera()

        # Pre-compute 2d steering vectors (array manifold)
        self._update_steering_vectors()

        # Statistics display
        self.mean_rssi = -np.inf
        self.mean_active_antennas = 0

        # List of recent MAC addresses
        self.recent_macs = set()

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "camera-ui.qml",
            {
                "WebCam": self.videocamera,
            },
        )

    def exec(self):
        # disable auto-focus and enable camera stream
        self.videocamera.setFocusMode(PyQt6.QtMultimedia.QCamera.FocusMode.FocusModeManual)
        self.videocamera.start()

        return super().exec()

    @PyQt6.QtCore.pyqtSlot()
    def updateSpatialSpectrum(self):
        if not hasattr(self, "backlog"):
            # No backlog available yet, demo has not fully initialized
            return

        try:
            csi_backlog, rssi_backlog, timestamp_backlog, mac_backlog, rfswitch_state_backlog = self.backlog.get_multiple(
                [
                    self.genericconfig.get("preamble_format"),
                    "rssi",
                    "host_timestamp",
                    "mac",
                    "rfswitch_state",
                ]
            )
        except ValueError as e:
            print(f"Error retrieving backlog data: {e}")
            return

        if csi_backlog.size == 0:
            # No data available yet
            return

        max_age = self.appconfig.get("beamformer", "max_age")
        if max_age > 0.0:
            csi_backlog[timestamp_backlog < (time.time() - max_age), ...] = 0
            recent_rssi_backlog = rssi_backlog[timestamp_backlog > (time.time() - max_age), ...]
        else:
            recent_rssi_backlog = rssi_backlog

        # Update mean RSSI
        self.mean_rssi = 10 * np.log10(np.nanmean(10 ** (recent_rssi_backlog / 10)) + 1e-6) if recent_rssi_backlog.size > 0 else -np.inf
        self.rssiChanged.emit(self.mean_rssi)

        # Update mean number of active antennas
        if recent_rssi_backlog.shape[0] > 0:
            self.mean_active_antennas = np.prod(recent_rssi_backlog.shape[1:]) - np.mean(np.sum(np.isnan(recent_rssi_backlog), axis=(1, 2, 3)))
            self.activeAntennasChanged.emit(self.mean_active_antennas)

        # Update list of recent MAC addresses
        # Only send signal if list of MAC addresses has changed
        # mac_backlog is a numpy array of shape (n_packets, 6) of data type uint8, where each row is a MAC address
        if self.appconfig.get("receiver", "mac_list_enabled"):
            mac_strings = ["{:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}".format(*mac) for mac in mac_backlog]
            mac_strings_set = set(mac_strings)

            # Check if set of stored recent MACs match current MACs exactly, including contents
            if self.recent_macs != mac_strings_set:
                self.recent_macs = mac_strings_set
                self.recentMacsChanged.emit(list(self.recent_macs))

        # CSI backlog may be incomplete: If individual sensor did not provide packet, CSI value is NaN
        # For the purpose of visualization, we treat these NaN values as 0
        csi_backlog = np.nan_to_num(csi_backlog, nan=0.0)
        rssi_backlog = np.nan_to_num(rssi_backlog, nan=-np.inf)

        espargos.util.remove_mean_sto(csi_backlog)

        # Apply additional calibration (only phase)
        if self.additional_calibration is not None:
            # TODO: espargos.pool should natively support additional calibration
            csi_backlog = np.einsum(
                "dbrcs,brcs->dbrcs",
                csi_backlog,
                np.exp(-1.0j * np.angle(self.additional_calibration)),
            )

        # Weight CSI data with RSSI
        csi_backlog = csi_backlog * 10 ** (rssi_backlog[..., np.newaxis] / 20)

        # Build combined array CSI data and add fake array index dimension
        csi_combined = espargos.util.build_combined_array_data(self.indexing_matrix, csi_backlog)
        csi_combined = csi_combined[:, np.newaxis]
        rfswitch_state_combined = espargos.util.build_combined_array_data(self.indexing_matrix, rfswitch_state_backlog)
        rfswitch_state_combined = rfswitch_state_combined[:, np.newaxis]

        # Get rid of gap in CSI data around DC
        match self.genericconfig.get("preamble_format"):
            case "ht20":
                espargos.util.interpolate_ht20ltf_gap(csi_combined)
            case "ht40":
                espargos.util.interpolate_ht40ltf_gap(csi_combined)

        # Shift all CSI datapoints in time so that LoS component arrives at the same time
        csi_combined = espargos.util.shift_to_firstpeak_sync(
            csi_combined,
            peak_threshold=(0.4 if self.genericconfig.get("preamble_format") == "lltf" else 0.1),
        )

        beamformer_type = self.appconfig.get("beamformer", "type")
        match beamformer_type:
            case "MUSIC" | "MVDR":
                # Option 1: MUSIC or MVDR spatial spectrum
                # Multipath can be resolved due to multiple subcarriers, which pfrovide sufficient decorelation
                # between different paths if delay spread is sufficiently large.
                # Compute array covariance matrix R. Flatten CSI over horizontal and vertical dimensions of array.
                csi_flat = csi_combined.reshape(
                    csi_combined.shape[0],
                    csi_combined.shape[1],
                    csi_combined.shape[2] * csi_combined.shape[3],
                    csi_combined.shape[4],
                )
                R = np.einsum("dbis,dbjs->ij", csi_flat, np.conj(csi_flat)) / (csi_flat.shape[0] * csi_flat.shape[1] * csi_flat.shape[3])
                self.beamspace_power = self._music_algorithm(R) if beamformer_type == "MUSIC" else self._mvdr_algorithm(R)

            # Option 2: Beamspace via FFT
            case "FFT":
                # csi_combined has shape (datapoints, boards, row, column, subcarriers)
                if self.appconfig.get("beamformer", "polarization_mode") != "ignore":
                    # Separate CSI by feed
                    csi_combined = espargos.util.separate_feeds(csi_combined, rfswitch_state_combined)  # (D, B, M, N, S, 2)
                    if csi_combined is None:
                        print("Must have measurements for both R and L feeds for polarization visualization")
                        return

                # For computational efficiency reasons, reduce number of datapoints to one by interpolating over all datapoints
                # This assumes a constant channel except for CFO-induced phase rotations and noise
                csi_combined = espargos.util.csi_interp_iterative(csi_combined, iterations=5)

                # Now csi_combined can have two possible shapes:
                # * Without polarization: (B=1, M, N, S)
                # * With polarization: (B=1, M, N, S, 2)

                # Exploit time-domain sparsity to reduce number of 2D FFTs from antenna space to beamspace
                csi_tdomain = np.fft.ifftshift(
                    np.fft.ifft(np.fft.fftshift(csi_combined, axes=3), axis=3),
                    axes=3,
                )
                tap_count = csi_tdomain.shape[3]
                # csi_tdomain_cut = csi_tdomain[..., tap_count // 2 + 1 - 16 : tap_count // 2 + 1 + 17]
                csi_tdomain_cut = csi_tdomain.take(range(tap_count // 2 + 1 - 16, tap_count // 2 + 1 + 17), axis=3)
                csi_fdomain_cut = np.fft.ifftshift(
                    np.fft.fft(np.fft.fftshift(csi_tdomain_cut, axes=3), axis=3),
                    axes=3,
                )

                # Here, we only go to DFT beamspace, not directly azimuth / elevation space,
                # but the shader can take care of fixing the distortion.
                # csi_zeropadded either has shape
                # * (azimuth / row, elevation / column, subcarriers) without polarization or
                # * (azimuth / row, elevation / column, subcarriers, 2) with polarization
                csi_zeropadded = np.zeros(
                    (self.appconfig.get("beamformer", "resolution_azimuth"), self.appconfig.get("beamformer", "resolution_elevation")) + csi_fdomain_cut.shape[3:],
                    dtype=csi_fdomain_cut.dtype,
                )
                real_rows_half = csi_fdomain_cut.shape[1] // 2
                real_cols_half = csi_fdomain_cut.shape[2] // 2
                zeropadded_rows_half = csi_zeropadded.shape[1] // 2
                zeropadded_cols_half = csi_zeropadded.shape[0] // 2
                csi_zeropadded[
                    zeropadded_cols_half - real_cols_half : zeropadded_cols_half + real_cols_half,
                    zeropadded_rows_half - real_rows_half : zeropadded_rows_half + real_rows_half,
                    :,
                ] = np.swapaxes(csi_fdomain_cut[0, ...], 0, 1)
                csi_zeropadded = np.fft.ifftshift(csi_zeropadded, axes=(0, 1))
                beam_frequency_space = np.fft.fft2(csi_zeropadded, axes=(0, 1))
                beam_frequency_space = np.fft.fftshift(beam_frequency_space, axes=(0, 1))

                if self.appconfig.get("beamformer", "polarization_mode") == "ignore":
                    # beam_frequency_space has shape (azimuth, elevation, subcarriers, 2)
                    # Compute total power over both polarizations
                    self.beamspace_power = np.mean(np.abs(beam_frequency_space) ** 2, axis=-1)
                else:
                    # We are now either in polarization_mode "show" or "incorporate"
                    # beam_frequency_space has shape (azimuth, elevation, subcarriers, 2)
                    # Separate polarizations into V/H components using antenna Jones matrix
                    beam_frequency_polarization = np.einsum("aesf,fp->aesp", beam_frequency_space, np.linalg.inv(espargos.constants.ANTENNA_JONES_MATRIX))

                    # For power (brightness) of visualization: Use total power over both polarizations
                    self.beamspace_power = np.mean(np.abs(beam_frequency_polarization) ** 2, axis=(-2, -1))

                    # For delay (color hue) of visualization:
                    # Move polarization axis to front, treating each as an independent observation
                    # The delay computation sums phase derivatives over axis 0, so both polarizations
                    # contribute their delay estimates constructively (no destructive interference)
                    beam_frequency_space = np.moveaxis(beam_frequency_space, -1, 0)

                    if self.appconfig.get("beamformer", "polarization_mode") == "show":
                        # Combine polarization information of all subcarriers into a single polarization estimate per beam
                        # using the polarization coherence matrix (2x2 Hermitian matrix per beam, summed over subcarriers).
                        # Delay across subcarriers cancels out because both polarization components share the same propagation delay.
                        # The dominant eigenvector of J gives the best polarization estimate.
                        # beam_frequency_polarization: (azimuth, elevation, subcarriers, 2)
                        # J: (azimuth, elevation, 2, 2)
                        J = np.einsum("aesp,aesq->aepq", beam_frequency_polarization, np.conj(beam_frequency_polarization))
                        eigenvalues, eigenvectors = np.linalg.eig(J)
                        max_eigenvalue_indices = np.argmax(eigenvalues, axis=-1)
                        polarization_estimate = eigenvectors[np.arange(eigenvectors.shape[0])[:, np.newaxis], np.arange(eigenvectors.shape[1]), max_eigenvalue_indices]

                        # debugging: just grab subcarrier from middle of band
                        #polarization_estimate = beam_frequency_polarization[:, :, beam_frequency_polarization.shape[2] // 2, :]

                        # Normalize so that total power is 1
                        polarization_estimate = polarization_estimate / (np.linalg.norm(polarization_estimate, axis=-1, keepdims=True) + 1e-12)

                        # inv(ANTENNA_JONES_MATRIX) converts from R/L feed basis to H/V linear basis,
                        # so index 0 = H and index 1 = V

                        # Normalize global phase so that V component (index 1) is real and non-negative.
                        # This means V only needs one real channel, freeing the alpha channel.
                        v_phase = np.angle(polarization_estimate[..., 1])
                        polarization_estimate = polarization_estimate * np.exp(-1j * v_phase)[..., np.newaxis]
                        # Flip sign if V ended up negative (angle was pi)
                        polarization_estimate = np.where(
                            polarization_estimate[..., 1:2].real < 0,
                            -polarization_estimate,
                            polarization_estimate,
                        )

                        v_amplitude = polarization_estimate[..., 1].real  # |V|, in [0, 1]
                        h_complex = polarization_estimate[..., 0]          # complex H

                        # Encode as RGBA texture:
                        # R = V amplitude [0,1] (V is real after phase normalization)
                        # G = H real part [-1,1] -> [0,1]
                        # B = H imag part [-1,1] -> [0,1]
                        # A = 255 (always opaque, avoids premultiplied alpha corruption)
                        self.polarization_imagedata = np.zeros(self.beamspace_power.size * 4, dtype=np.uint8)
                        self.polarization_imagedata[0::4] = np.clip(np.swapaxes(v_amplitude, 0, 1).ravel(), 0, 1) * 255
                        self.polarization_imagedata[1::4] = np.clip(np.swapaxes((h_complex.real + 1.0) / 2.0, 0, 1).ravel(), 0, 1) * 255
                        self.polarization_imagedata[2::4] = np.clip(np.swapaxes((h_complex.imag + 1.0) / 2.0, 0, 1).ravel(), 0, 1) * 255
                        self.polarization_imagedata[3::4] = 255
                        self.polarizationImagedataChanged.emit(self.polarization_imagedata.tolist())

                        # TODO
                        pol_ratio = np.swapaxes(np.abs(polarization_estimate[:, :, 0]) / np.abs(polarization_estimate[:, :, 1]), 0, 1).ravel()
                        #phase_ratio = np.swapaxes(np.angle(polarization_estimate[:, :, 0]) - np.angle(polarization_estimate[:, :, 1]), 0, 1).ravel()
                        #print(phase_ratio)
                        power_visualization_beamspace = self.beamspace_power**3
                        relative_power = power_visualization_beamspace / (np.max(power_visualization_beamspace) + 1e-6)
                        self.beamspace_power_imagedata = np.zeros(4 * self.beamspace_power.size, dtype=np.uint8)
                        power_color = np.clip(np.swapaxes(relative_power, 0, 1).ravel(), 0, 1) * 255
                        self.beamspace_power_imagedata[0::4] = np.where((1/pol_ratio<1), power_color * (1/pol_ratio), power_color)
                        self.beamspace_power_imagedata[1::4] = np.where(pol_ratio < 1, power_color * pol_ratio, power_color)
                        self.beamspace_power_imagedata[3::4] = 255

                        self.beamspacePowerImagedataChanged.emit(self.beamspace_power_imagedata.tolist())
                        return


            case "Bartlett":
                # For computational efficiency reasons, reduce number of datapoints to one by interpolating over all datapoints
                # This assumes a constant channel except for CFO-induced phase rotations and noise
                csi_combined = np.asarray([espargos.util.csi_interp_iterative(csi_combined, iterations=5)])

                # Compute sum of received power per steering angle over all datapoints and subcarriers
                # real 2d spatial spectrum is too slow...
                # we can use 2D FFT to get to beamspace, which of course is technically not correct
                # (cannot separate 2D steering vector into Kronecker product of azimuth / elevation steering vectors)
                beam_frequency_space = np.einsum(
                    "rcae,dbrcs->daes",
                    np.conj(self.steering_vectors_2d),
                    csi_combined,
                    optimize=True,
                )
                self.beamspace_power = np.mean(np.abs(beam_frequency_space) ** 2, axis=(0, 3))

        if self.appconfig.get("visualization", "overlay") == "Power":
            db_beamspace = 10 * np.log10(self.beamspace_power + 1e-6)
            db_beamspace_norm = (db_beamspace - np.max(db_beamspace) + 15) / 15
            db_beamspace_norm = np.clip(db_beamspace_norm, 0, 1)
            color_beamspace = self._viridis(db_beamspace_norm)

            alpha_channel = np.ones((*color_beamspace.shape[:2], 1))
            color_beamspace_rgba = np.clip(np.concatenate((color_beamspace, alpha_channel), axis=-1), 0, 1)
            self.beamspace_power_imagedata = np.asarray(np.swapaxes(color_beamspace_rgba, 0, 1).ravel() * 255, dtype=np.uint8)
        else:
            power_visualization_beamspace = self.beamspace_power**3

            if self.appconfig.get("visualization", "manual_exposure"):
                match self.appconfig.get("beamformer", "type"):
                    case "MUSIC" | "MVDR":
                        value_range = 1e-2
                    case "FFT" | "Bartlett":
                        value_range = 1e11
                exposure = self.appconfig.get("visualization", "exposure")
                color_value = power_visualization_beamspace / value_range * (10 ** (exposure / 0.1) + 1e-6)
            else:
                color_value = power_visualization_beamspace / (np.max(power_visualization_beamspace) + 1e-6)

            if self.appconfig.get("beamformer", "colorize_delay"):
                if self.appconfig.get("beamformer", "type") in ["MUSIC", "MVDR"]:
                    raise NotImplementedError("Delay colorization not supported in MUSIC or MVDR mode")

                # Ensure beam_frequency_space is 4D: (observations, azimuth, elevation, subcarriers)
                # For FFT with polarization_mode="show", it's already (2, az, el, sc) after moveaxis
                # For FFT with polarization_mode="ignore", it's (az, el, sc) - add fake axis
                # For Bartlett, it's (datapoints, az, el, sc)
                if beam_frequency_space.ndim == 3:
                    beam_frequency_space = beam_frequency_space[np.newaxis, ...]

                # Compute beam powers and delay. Beam power is value, delay is hue.
                beamspace_weighted_delay_phase = np.sum(
                    beam_frequency_space[..., 1:] * np.conj(beam_frequency_space[..., :-1]),
                    axis=(0, -1),
                )
                delay_by_beam = np.angle(beamspace_weighted_delay_phase)
                mean_delay = np.angle(np.sum(beamspace_weighted_delay_phase))

                hsv = np.zeros((beam_frequency_space.shape[1], beam_frequency_space.shape[2], 3))
                hsv[:, :, 0] = (
                    np.clip(
                        (delay_by_beam - mean_delay) / self.appconfig.get("beamformer", "max_delay"),
                        0,
                        1,
                    )
                    + 1 / 3
                ) % 1.0
                hsv[:, :, 1] = 0.8
                hsv[:, :, 2] = color_value

                wifi_image_rgb = matplotlib.colors.hsv_to_rgb(hsv)
                alpha_channel = np.ones((*wifi_image_rgb.shape[:2], 1))
                wifi_image_rgba = np.clip(np.concatenate((wifi_image_rgb, alpha_channel), axis=-1), 0, 1)
                self.beamspace_power_imagedata = np.asarray(np.swapaxes(wifi_image_rgba, 0, 1).ravel() * 255, dtype=np.uint8)
            else:
                self.beamspace_power_imagedata = np.zeros(4 * self.beamspace_power.size, dtype=np.uint8)
                self.beamspace_power_imagedata[1::4] = np.clip(np.swapaxes(color_value, 0, 1).ravel(), 0, 1) * 255
                self.beamspace_power_imagedata[3::4] = 255

        self.beamspacePowerImagedataChanged.emit(self.beamspace_power_imagedata.tolist())

    def _music_algorithm(self, R):
        # Compute spatial spectrum using MUSIC algorithm based on R
        # For the relatively small arrays, eig is faster than eigh
        steering_vectors_2d_flat = self.steering_vectors_2d.reshape(-1, self.steering_vectors_2d.shape[2], self.steering_vectors_2d.shape[3])
        R = (R + np.conj(R.T)) / 2
        eig_val, eig_vec = np.linalg.eig(R)
        order = np.argsort(eig_val)[::-1]
        # TODO: Estimate number of sources, or use user-defined number of sources
        Qn = eig_vec[:, order][:, 1:]
        spatial_spectrum = 1 / np.linalg.norm(np.einsum("ae,a...->e...", Qn, np.conj(steering_vectors_2d_flat)), axis=0)

        return spatial_spectrum - np.min(spatial_spectrum) + 1e-6

    def _mvdr_algorithm(self, R):
        # Compute spatial spectrum using MVDR algorithm based on R
        steering_vectors_2d_flat = self.steering_vectors_2d.reshape(-1, self.steering_vectors_2d.shape[2], self.steering_vectors_2d.shape[3])

        # MVDR is sensitive to ill-conditioned covariance; use diagonal loading and symmetrization
        R = (R + np.conj(R.T)) / 2
        loading = 0.1 * np.trace(R) / R.shape[0]
        R_loaded = R + loading * np.eye(R.shape[0])
        R_inv = np.linalg.pinv(R_loaded)
        denom = np.einsum(
            "a...,ab,b...->...",
            np.conj(steering_vectors_2d_flat),
            R_inv,
            steering_vectors_2d_flat,
        )
        spatial_spectrum = 1.0 / np.maximum(np.real(denom), 1e-12)

        return spatial_spectrum - np.min(spatial_spectrum) + 1e-6

    def _viridis(self, values):
        viridis_colormap = np.asarray(
            [
                (0.267004, 0.004874, 0.329415),
                (0.229739, 0.322361, 0.545706),
                (0.127568, 0.566949, 0.550556),
                (0.369214, 0.788888, 0.382914),
                (0.993248, 0.906157, 0.143936),
                (0.993248, 0.906157, 0.143936),
            ]
        )

        n = len(viridis_colormap) - 1
        idx = values * n
        low = np.floor(idx).astype(int)
        high = np.ceil(idx).astype(int)
        t = idx - low

        c0 = viridis_colormap[low]
        c1 = viridis_colormap[high]

        return c0 * (1 - t[:, :, np.newaxis]) + c1 * t[:, :, np.newaxis]

    def _update_steering_vectors(self):
        resolution_azimuth = self.appconfig.get("beamformer", "resolution_azimuth")
        resolution_elevation = self.appconfig.get("beamformer", "resolution_elevation")
        phase_c = np.outer(np.arange(self.n_cols), np.linspace(-np.pi, np.pi, resolution_azimuth))
        phase_r = np.outer(np.arange(self.n_rows), np.linspace(-np.pi, np.pi, resolution_elevation))
        self.steering_vectors_2d = np.exp(1.0j * (phase_c[np.newaxis, :, :, np.newaxis] + phase_r[:, np.newaxis, np.newaxis, :]))

    def _cb_predicate(self, csi_completion_state, csi_age):
        timeout_condition = False
        if self.args.csi_completion_timeout > 0:
            timeout_condition = np.sum(csi_completion_state) >= 2 and csi_age > self.args.csi_completion_timeout

        return np.all(csi_completion_state) or timeout_condition

    def onAboutToQuit(self):
        self.videocamera.stop()
        super().onAboutToQuit()

    @PyQt6.QtCore.pyqtSlot(dict)
    def _on_update_app_state(self, newcfg):
        camera_cfg = newcfg.get("camera", {}) if isinstance(newcfg, dict) else {}
        if not isinstance(camera_cfg, dict):
            camera_cfg = {}

        # Only update camera settings if changed
        if "device" in camera_cfg:
            try:
                self.videocamera.setDevice(camera_cfg.get("device"))
            except Exception as e:
                print(f"Error setting camera device: {e}")

        if "format" in camera_cfg:
            try:
                self.videocamera.setFormat(camera_cfg.get("format"))
            except Exception as e:
                print(f"Error setting camera format: {e}")

        if "flip" in camera_cfg:
            try:
                self.cameraFlipChanged.emit()
            except Exception as e:
                print(f"Error setting camera flip: {e}")

        if "fov_azimuth" in camera_cfg:
            try:
                self.fovAzimuthChanged.emit()
            except Exception as e:
                print(f"Error setting camera fov azimuth: {e}")

        if "fov_elevation" in camera_cfg:
            try:
                self.fovElevationChanged.emit()
            except Exception as e:
                print(f"Error setting camera fov elevation: {e}")

        if "receiver" in newcfg:
            receiver_cfg = newcfg.get("receiver", {}) if isinstance(newcfg.get("receiver", {}), dict) else {}
            if "mac_list_enabled" in receiver_cfg:
                try:
                    self.macListEnabledChanged.emit()
                except Exception as e:
                    print(f"Error setting mac list feature: {e}")

        if "beamformer" in newcfg:
            beamformer_cfg = newcfg.get("beamformer", {}) if isinstance(newcfg.get("beamformer", {}), dict) else {}
            if "resolution_azimuth" in beamformer_cfg or "resolution_elevation" in beamformer_cfg:
                try:
                    self._update_steering_vectors()
                    if "resolution_azimuth" in beamformer_cfg:
                        self.resolutionAzimuthChanged.emit()
                    if "resolution_elevation" in beamformer_cfg:
                        self.resolutionElevationChanged.emit()
                except Exception as e:
                    print(f"Error setting beamformer resolution: {e}")

        if "visualization" in newcfg and "space" in newcfg["visualization"]:
            try:
                self.rawBeamspaceChanged.emit()
            except Exception as e:
                print(f"Error setting raw beamspace: {e}")

        # Let base class handle the rest
        super()._on_update_app_state(newcfg)

    @PyQt6.QtCore.pyqtProperty(bool, constant=True)
    def music(self):
        return self.appconfig.get("beamformer", "type") == "MUSIC"

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=resolutionAzimuthChanged)
    def resolutionAzimuth(self):
        return self.appconfig.get("beamformer", "resolution_azimuth")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=resolutionElevationChanged)
    def resolutionElevation(self):
        return self.appconfig.get("beamformer", "resolution_elevation")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=fovAzimuthChanged)
    def fovAzimuth(self):
        return self.appconfig.get("camera", "fov_azimuth")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=fovElevationChanged)
    def fovElevation(self):
        return self.appconfig.get("camera", "fov_elevation")

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=rawBeamspaceChanged)
    def rawBeamspace(self):
        return self.appconfig.get("visualization", "space")

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=rssiChanged)
    def rssi(self):
        return self.mean_rssi

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=activeAntennasChanged)
    def activeAntennas(self):
        return self.mean_active_antennas

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=macListEnabledChanged)
    def macListEnabled(self):
        return self.appconfig.get("receiver", "mac_list_enabled")

    @PyQt6.QtCore.pyqtProperty(list, constant=False, notify=recentMacsChanged)
    def macList(self):
        return self.recent_macs

    @PyQt6.QtCore.pyqtSlot(str)
    def setMacFilter(self, mac):
        self.pool.set_mac_filter({"enable": True, "mac": mac})
        self.pooldrawer.configManager().set({"mac_filter": {"enable": True, "mac": mac}})

    @PyQt6.QtCore.pyqtSlot()
    def clearMacFilter(self):
        self.pool.clear_mac_filter()
        self.pooldrawer.configManager().set({"mac_filter": {"enable": False}})

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=cameraFlipChanged)
    def cameraFlip(self):
        return self.appconfig.get("camera", "flip")


app = EspargosDemoCamera(sys.argv)
sys.exit(app.exec())
