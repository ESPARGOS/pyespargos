#!/usr/bin/env python

"""WiFi CSI implementation of the reusable camera visualization."""

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import CSIBacklogMixin, CameraView, CombinedArrayMixin, ESPARGOSCSIApplication, SingleCSIFormatMixin
import numpy as np
import espargos
import time

import PyQt6.QtCore

try:
    from .csi_overlay import CSIOverlay
except ImportError:
    from csi_overlay import CSIOverlay


class CSICameraDemo(CSIBacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin, ESPARGOSCSIApplication):
    """Acquire CSI, compute its spatial spectrum, and publish it to the camera UI."""

    DEFAULT_CONFIG = {
        "receiver": {"mac_list_enabled": False},
        "camera": CameraView.DEFAULT_CONFIG,
        "beamformer": {
            "type": "FFT",
            "colorize_delay": False,
            "polarization_mode": "ignore",
            "grid_spacing": 24,
            "normalize_polarization": False,
            "max_delay": 0.2,
            "max_age": 0.0,
            "resolution_azimuth": 64,
            "resolution_elevation": 32,
        },
        "visualization": {
            "space": "camera",
            "overlay": "Default",
            "manual_exposure": False,
            "exposure": 0.5,
            "azimuth_correction": 0.0,
            "elevation_correction": 0.0,
        },
    }

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        parser.add_argument(
            "--no-camera",
            default=False,
            help="Do not use a video camera; show only the spatial visualization",
            action="store_true",
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
            help="Time after which a CSI cluster is accepted with missing receivers; zero disables incomplete clusters",
        )

    def __init__(self, argv):
        super().__init__(argv)
        if self.args.no_camera:
            self.appconfig.set({"camera": {"enable": False}})

        self.additional_calibration = None
        if self.args.additional_calibration:
            self.additional_calibration = np.load(self.args.additional_calibration)

        self.initialize_pool(backlog_cb_predicate=self._cb_predicate)
        self._update_steering_vectors()
        self.jones_matrices_inv = espargos.util.build_jones_matrices(self.antenna_orientations)

        self.camera_view = CameraView(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        self.overlay = CSIOverlay(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        video_camera = self.camera_view.initialize()
        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "camera-ui.qml",
            {
                "CameraView": self.camera_view,
                "overlayModel": self.overlay,
                "WebCam": video_camera,
            },
        )

    def exec(self):
        self.camera_view.start()
        return super().exec()

    @PyQt6.QtCore.pyqtSlot()
    def updateSpatialSpectrum(self):
        result = self.get_backlog_csi(
            "rssi",
            "rx_gain",
            "fft_gain",
            "host_timestamp",
            "mac",
            "rfswitch_state",
            allow_incomplete=True,
            remove_global_sto=False,
            return_format=True,
        )
        if result is None:
            return
        csi_format, csi, rssi, rx_gain, fft_gain, timestamps, macs, receiver_state = result

        max_age = self.appconfig.get("beamformer", "max_age")
        if max_age > 0.0:
            recent = timestamps > (time.time() - max_age)
            csi[~recent, ...] = 0
            recent_rssi = rssi[recent, ...]
        else:
            recent_rssi = rssi

        mean_power = 10 * np.log10(np.nanmean(10 ** (recent_rssi / 10)) + 1e-15) if recent_rssi.size else -np.inf
        active_receivers = 0.0
        if recent_rssi.shape[0] > 0:
            active_receivers = np.prod(recent_rssi.shape[1:]) - np.mean(np.sum(np.isnan(recent_rssi), axis=(1, 2, 3)))
        self.overlay.publish_receiver_statistics(mean_power, "dBm", active_receivers)

        if self.appconfig.get("receiver", "mac_list_enabled"):
            self.overlay.publish_mac_addresses("{:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}".format(*mac) for mac in macs)

        espargos.util.remove_mean_sto(csi)
        if self.additional_calibration is not None:
            csi = np.einsum(
                "dbrcs,brcs->dbrcs",
                csi,
                np.exp(-1.0j * np.angle(self.additional_calibration)),
            )
        csi = espargos.util.scale_csi_by_reported_gain(csi, rx_gain, fft_gain)
        csi = np.nan_to_num(csi, nan=0.0)

        csi_combined = espargos.util.build_combined_array_data(self.indexing_matrix, csi)
        csi_combined = csi_combined[:, np.newaxis]
        receiver_state_combined = espargos.util.build_combined_array_data(self.indexing_matrix, receiver_state)
        receiver_state_combined = receiver_state_combined[:, np.newaxis]
        csi_combined = espargos.util.shift_to_firstpeak_sync(
            csi_combined,
            peak_threshold=(0.4 if csi_format == "lltf" else 0.1),
        )

        beam_frequency_space = None
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
                self.beamspace_power = espargos.util.music_spectrum(R, self.steering_vectors_2d) if beamformer_type == "MUSIC" else espargos.util.mvdr_spectrum(R, self.steering_vectors_2d)

            # Option 2: Beamspace via FFT
            case "FFT":
                # csi_combined has shape (datapoints, boards, row, column, subcarriers)
                if self.appconfig.get("beamformer", "polarization_mode") != "ignore":
                    if receiver_state_combined is None:
                        print("Receiver-state metadata is required for polarization visualization")
                        return
                    # Separate CSI by feed
                    csi_combined = espargos.util.separate_feeds(csi_combined, receiver_state_combined)  # (D, B, M, N, S, 2)
                    if csi_combined is None:
                        print("Must have measurements for both R and L feeds for polarization visualization")
                        return

                    # Apply per-antenna Jones correction to convert R/L feeds to global H/V polarization
                    # in the antenna domain (before beamforming), since each antenna may be rotated differently.
                    # csi_combined: (D, B=1, M, N, S, 2) where last dim is R/L
                    # jones_matrices_inv: (M, N, 2, 2) maps R/L -> global H/V
                    csi_combined = np.einsum("dbmnsf,mnfp->dbmnsp", csi_combined, self.jones_matrices_inv)

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

                # Normalize by number of antenna elements so that beamspace power is
                # independent of array size and comparable across beamformer types
                beam_frequency_space = beam_frequency_space / (self.n_rows * self.n_cols)

                if self.appconfig.get("beamformer", "polarization_mode") == "ignore":
                    # beam_frequency_space has shape (azimuth, elevation, subcarriers, 2)
                    # Compute total power over both polarizations
                    self.beamspace_power = np.mean(np.abs(beam_frequency_space) ** 2, axis=-1)
                else:
                    # We are now either in polarization_mode "show" or "incorporate"
                    # beam_frequency_space has shape (azimuth, elevation, subcarriers, 2)
                    # Data is already in global H/V basis (per-antenna Jones correction applied before beamforming),
                    # so index 0 = H and index 1 = V

                    # For power (brightness) of visualization: Use total power over both polarizations
                    self.beamspace_power = np.mean(np.abs(beam_frequency_space) ** 2, axis=(-2, -1))

                    if self.appconfig.get("beamformer", "polarization_mode") == "show":
                        # Combine polarization information of all subcarriers into a single polarization estimate per beam
                        # while preserving the relative phase (and thus sign) of the beamformed signal between beams.
                        # beam_frequency_space: (azimuth, elevation, subcarriers, 2)
                        #
                        # A local coherency/eigenvector estimate would remove the scalar phase independently
                        # for every beam and therefore hide global sign/phase differences between source
                        # directions. Instead, estimate the local Jones vector using a fixed polarization
                        # component as phase gauge, then reattach the phase of the same component at one
                        # reference subcarrier. This removes arbitrary scalar frequency dependence without
                        # assuming a linear phase/delay model, while still making relative phases between
                        # beams visible.

                        # Find globally dominant polarization component (H=0, V=1) for phase reference
                        total_power_per_component = np.sum(np.abs(beam_frequency_space) ** 2, axis=(0, 1, 2))
                        ref_comp = np.argmax(total_power_per_component)

                        # Estimate the local Jones vector with a reference-aware rank-one fit:
                        #   eta_ref ~ sum_k b_k b_{k,ref}^*
                        # This fixes the local polarization gauge through the chosen reference component.
                        ref = beam_frequency_space[..., ref_comp]  # (az, el, subcarriers)
                        polarization_estimate = np.sum(beam_frequency_space * np.conj(ref)[..., np.newaxis], axis=2)  # (az, el, 2)

                        # Normalize so that total power is 1
                        polarization_estimate = polarization_estimate / (np.linalg.norm(polarization_estimate, axis=-1, keepdims=True) + 1e-12)

                        # Reattach the globally meaningful phase of the reference component at the center
                        # subcarrier. This displays exp(j arg(alpha_k0)) * eta rather than eta alone, so
                        # opposite signs and other relative beam phases remain visible.
                        center_sc = beam_frequency_space.shape[-2] // 2
                        beam_phase_reference = np.angle(beam_frequency_space[..., center_sc, ref_comp])  # (az, el)
                        polarization_estimate = polarization_estimate * np.exp(1j * beam_phase_reference)[..., np.newaxis]

                        # Apply a single global phase rotation to make V predominantly real.
                        # Use power-weighted aggregate to determine the global V phase.
                        # This is a single rotation for all pixels, so relative phases between
                        # different directions are preserved (including sign differences).
                        v_global_phase = np.angle(np.sum(polarization_estimate[..., 1] * self.beamspace_power))
                        polarization_estimate = polarization_estimate * np.exp(-1j * v_global_phase)
                        if np.sum(polarization_estimate[..., 1].real * self.beamspace_power) < 0:
                            polarization_estimate = -polarization_estimate

                        # V is now predominantly real after the global phase rotation.
                        # Individual pixels may have V < 0 (opposite sign from dominant source) — this is
                        # real physical information that we preserve. V may also have small imaginary
                        # residuals which we discard (second-order effect from global phase approximation).
                        v_signed = polarization_estimate[..., 1].real  # signed, in [-1, 1]
                        h_complex = polarization_estimate[..., 0]  # complex H

                        # Compute per-pixel power scaling for un-normalized mode.
                        # Scale polarization components by sqrt(power/max_power) so oscillation
                        # amplitude reflects received power. Applied before encoding so no
                        # shader-side logic is needed.
                        if not self.appconfig.get("beamformer", "normalize_polarization"):
                            power_scale = np.sqrt(self.beamspace_power / (np.max(self.beamspace_power) + 1e-12))
                            v_signed = v_signed * power_scale
                            h_complex = h_complex * power_scale

                        self.overlay.publish_polarization(v_signed, h_complex)

                    # For delay colorization: move polarization axis to front so both H/V are treated
                    # as independent observations. The delay computation sums phase derivatives over
                    # axis 0, so both polarizations contribute constructively.
                    beam_frequency_space = np.moveaxis(beam_frequency_space, -1, 0)

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

                # Normalize by number of antenna elements so that beamspace power is
                # independent of array size and comparable across beamformer types
                beam_frequency_space = beam_frequency_space / (self.n_rows * self.n_cols)
                self.beamspace_power = np.mean(np.abs(beam_frequency_space) ** 2, axis=(0, 3))

        self.overlay.publish_spatial_spectrum(self.beamspace_power, beam_frequency_space)

    def _cb_predicate(self, cluster):
        csi_completion_state = cluster.get_completion()
        timeout_condition = False
        if self.args.csi_completion_timeout > 0:
            timeout_condition = np.sum(csi_completion_state) >= 2 and cluster.get_age() > self.args.csi_completion_timeout

        return np.all(csi_completion_state) or timeout_condition

    def _update_steering_vectors(self):
        self.steering_vectors_2d = espargos.util.steering_vectors_2d(
            self.n_rows,
            self.n_cols,
            self.appconfig.get("beamformer", "resolution_azimuth"),
            self.appconfig.get("beamformer", "resolution_elevation"),
        )

    @PyQt6.QtCore.pyqtSlot(dict)
    def _on_update_app_state(self, newcfg):
        beamformer_cfg = newcfg.get("beamformer", {}) if isinstance(newcfg, dict) else {}
        if isinstance(beamformer_cfg, dict) and ("resolution_azimuth" in beamformer_cfg or "resolution_elevation" in beamformer_cfg):
            if hasattr(self, "n_rows"):
                self._update_steering_vectors()
        super()._on_update_app_state(newcfg)

    def onAboutToQuit(self):
        if hasattr(self, "camera_view"):
            self.camera_view.stop()
        super().onAboutToQuit()

    @PyQt6.QtCore.pyqtSlot(str)
    def setMacFilter(self, mac):
        self.pool.set_mac_filter({"enable": True, "mac": mac})
        self.pooldrawer.configManager().set({"mac_filter": {"enable": True, "mac": mac}})

    @PyQt6.QtCore.pyqtSlot()
    def clearMacFilter(self):
        self.pool.clear_mac_filter()
        self.pooldrawer.configManager().set({"mac_filter": {"enable": False}})


EspargosDemoCamera = CSICameraDemo


def main(argv=None):
    app = CSICameraDemo(sys.argv if argv is None else argv)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
