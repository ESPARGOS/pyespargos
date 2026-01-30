#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ConfigManager
from demos.common import DemoApplication

import matplotlib.colors
import numpy as np
import espargos
import argparse
import time

import PyQt6.QtMultimedia
import PyQt6.QtCore
import PyQt6.QtQml

import videocamera

class EspargosDemoCamera(DemoApplication):
    rssiChanged = PyQt6.QtCore.pyqtSignal(float)
    activeAntennasChanged = PyQt6.QtCore.pyqtSignal(float)
    beamspacePowerImagedataChanged = PyQt6.QtCore.pyqtSignal(list)
    recentMacsChanged = PyQt6.QtCore.pyqtSignal(list)
    cameraFlipChanged = PyQt6.QtCore.pyqtSignal()
    rawBeamspaceChanged = PyQt6.QtCore.pyqtSignal()
    fovAzimuthChanged = PyQt6.QtCore.pyqtSignal()
    fovElevationChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
            "camera" : {
                "flip" : False,
                "format" : None, # will be populated by app, can take values like "1920x1080 @ 30.00 FPS",
                "device" : None, # will be populated by app, can take values like "/dev/video0"
                "fov_azimuth" : 72,
                "fov_elevation" : 41
            },
            "beamformer" : {
                "type" : "FFT",
                "colorize_delay" : False,
                "max_delay" : 0.2
            },
            "visualization" : {
                "space" : "Camera",
                "overlay" : "Default"
            }
        }

    def __init__(self, argv):
        super().__init__(argv)

        # Parse command line arguments
        parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Overlay received power on top of camera image", parents = [self.common_args])
        parser.add_argument("-b", "--backlog", type = int, default = 20, help = "Number of CSI datapoints to average over in backlog")
        parser.add_argument("-ra", "--resolution-azimuth", type = int, default = 64, help = "Beamspace resolution for azimuth angle")
        parser.add_argument("-re", "--resolution-elevation", type = int, default = 32, help = "Beamspace resolution for elevation angle")
        parser.add_argument("-a", "--additional-calibration", type = str, default = "", help = "File to read additional phase calibration results from")
        parser.add_argument("-e", "--manual-exposure", default = False, help = "Use manual exposure / brightness control for WiFi overlay", action = "store_true")
        parser.add_argument("--mac-filter", type = str, default = "", help = "Only display CSI data from given MAC address")
        parser.add_argument("--max-age", type = float, default = 0.0, help = "Limit maximum age of CSI data to this value (in seconds). Set to 0.0 to disable.")
        parser.add_argument("--csi-completion-timeout", type = float, default = 0.2, help = "Time after which CSI cluster is considered complete even if not all antennas have provided data. Set to zero to disable processing incomplete clusters.")
        parser.add_argument("--mac-list", default = False, help = "Display list of MAC addresses of available transmitters", action = "store_true")
        format_group = parser.add_mutually_exclusive_group()
        format_group.add_argument("-l", "--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
        format_group.add_argument("-ht40", "--ht40", default = False, help = "Use only CSI from HT40", action = "store_true")
        format_group.add_argument("-ht20", "--ht20", default = False, help = "Use only CSI from HT20", action = "store_true")
        self.args = self.parse_args(parser)

        # Check if at least one format is selected
        if not (self.args.lltf or self.args.ht40 or self.args.ht20):
            print("Error: At least one of --lltf, --ht40 or --ht20 must be selected.")
            sys.exit(1)

        # Load additional calibration data from file, if provided
        self.additional_calibration = None
        if len(self.args.additional_calibration) > 0:
            self.additional_calibration = np.load(self.args.additional_calibration)

        # Initialize combined array setup
        self.initialize_combined_array(enable_backlog = True, backlog_cb_predicate = self._cb_predicate)

        # Demo configuration manager
        self.democonfig = ConfigManager(self.DEFAULT_CONFIG, self.DEFAULT_CONFIG, parent = self)
        self.democonfig.updateAppState.connect(self.onUpdateAppState)

        # Apply optional YAML config to pool/demo config managers
        self.democonfig.set(self.get_initial_config("demo", default = {}))

        # Qt setup
        self.aboutToQuit.connect(self.onAboutToQuit)
        self.engine = PyQt6.QtQml.QQmlApplicationEngine()

        # Camera setup
        self.videocamera = videocamera.VideoCamera(self.democonfig.get("camera", "device"), self.democonfig.get("camera", "format"))

        # Let UI know about currently selected camera device and format
        self.democonfig.set({
            "camera" : {
                "device" : self.videocamera.getDevice(),
                "format" : self.videocamera.getFormat()
            }
        })

        # Pre-compute 2d steering vectors (array manifold)
        phase_c = np.outer(np.arange(self.n_cols), np.linspace(-np.pi, np.pi, self.args.resolution_azimuth))
        phase_r = np.outer(np.arange(self.n_rows), np.linspace(-np.pi, np.pi, self.args.resolution_elevation))
        self.steering_vectors_2d = np.exp(1.0j * (phase_c[np.newaxis,:, :, np.newaxis] + phase_r[:,np.newaxis,np.newaxis,:]))

        # Manual exposure control (only used if manual exposure is enabled)
        self.exposure = 0

        # Statistics display
        self.mean_rssi = -np.inf
        self.mean_active_antennas = 0

        # List of recent MAC addresses
        self.recent_macs = set()

    def exec(self):
        context = self.engine.rootContext()
        context.setContextProperty("backend", self)
        context.setContextProperty("poolconfig", self.pooldrawer.configManager())
        context.setContextProperty("democonfig", self.democonfig)
        context.setContextProperty("WebCam", self.videocamera)

        qmlFile = pathlib.Path(__file__).resolve().parent / "camera-ui.qml"
        self.engine.load(qmlFile.as_uri())
        if not self.engine.rootObjects():
            return -1

        # disable auto-focus and enable camera stream
        self.videocamera.setFocusMode(PyQt6.QtMultimedia.QCamera.FocusMode.FocusModeManual)
        self.videocamera.start()

        return super().exec()

    @PyQt6.QtCore.pyqtSlot()
    def updateSpatialSpectrum(self):
        if not hasattr(self, "backlog"):
            # No backlog available yet, demo has not fully initialized
            return

        self.backlog.read_start()
        csi_backlog = None

        if self.args.lltf:
            csi_backlog = self.backlog.get_lltf()
        elif self.args.ht40:
            csi_backlog = self.backlog.get_ht40()
        else:
            csi_backlog = self.backlog.get_ht20()

        rssi_backlog = self.backlog.get_rssi()
        timestamp_backlog = self.backlog.get_host_timestamps()
        mac_backlog = self.backlog.get_macs()
        self.backlog.read_finish()

        if self.args.max_age > 0.0:
            csi_backlog[timestamp_backlog < (time.time() - self.args.max_age),...] = 0
            recent_rssi_backlog = rssi_backlog[timestamp_backlog > (time.time() - self.args.max_age),...]
        else:
            recent_rssi_backlog = rssi_backlog

        # Update mean RSSI
        self.mean_rssi = 10 * np.log10(np.nanmean(10**(recent_rssi_backlog / 10)) + 1e-6) if recent_rssi_backlog.size > 0 else -np.inf
        self.rssiChanged.emit(self.mean_rssi)

        # Update mean number of active antennas
        if recent_rssi_backlog.shape[0] > 0:
            self.mean_active_antennas = np.prod(recent_rssi_backlog.shape[1:]) - np.mean(np.sum(np.isnan(recent_rssi_backlog), axis = (1, 2, 3)))
            self.activeAntennasChanged.emit(self.mean_active_antennas)

        # Update list of recent MAC addresses
        # Only send signal if list of MAC addresses has changed
        # mac_backlog is a numpy array of shape (n_packets, 6) of data type uint8, where each row is a MAC address
        if self.args.mac_list:
            mac_strings = ["{:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}".format(*mac) for mac in mac_backlog]
            mac_strings_set = set(mac_strings)

            # Check if set of stored recent MACs match current MACs exactly, including contents
            if self.recent_macs != mac_strings_set:
                self.recent_macs = mac_strings_set
                self.recentMacsChanged.emit(list(self.recent_macs))

        # CSI backlog may be incomplete: If individual sensor did not provide packet, CSI value is NaN
        # For the purpose of visualization, we treat these NaN values as 0
        csi_backlog = np.nan_to_num(csi_backlog, nan = 0.0)
        rssi_backlog = np.nan_to_num(rssi_backlog, nan = -np.inf)

        espargos.util.remove_mean_sto(csi_backlog)

        # Apply additional calibration (only phase)
        if self.additional_calibration is not None:
            # TODO: espargos.pool should natively support additional calibration
            csi_backlog = np.einsum("dbrcs,brcs->dbrcs", csi_backlog, np.exp(-1.0j * np.angle(self.additional_calibration)))

        # Weight CSI data with RSSI
        csi_backlog = csi_backlog * 10**(rssi_backlog[..., np.newaxis] / 20)

        # Build combined array CSI data and add fake array index dimension
        csi_combined = espargos.util.build_combined_array_csi(self.indexing_matrix, csi_backlog)
        csi_combined = csi_combined[:,np.newaxis,:,:,:]

        # Get rid of gap in CSI data around DC
        if self.args.lltf:
            espargos.util.interpolate_lltf_gap(csi_combined)
        elif self.args.ht20:
            espargos.util.interpolate_ht20ltf_gap(csi_combined)
        elif self.args.ht40:
            espargos.util.interpolate_ht40ltf_gap(csi_combined)

        # Shift all CSI datapoints in time so that LoS component arrives at the same time
        csi_combined = espargos.util.shift_to_firstpeak_sync(csi_combined, peak_threshold = (0.4 if self.args.lltf else 0.1))
        
        beamformer_type = self.democonfig.get("beamformer", "type")
        match beamformer_type:
            case "MUSIC" | "MVDR":
                # Option 1: MUSIC or MVDR spatial spectrum
                # Multipath can be resolved due to multiple subcarriers, which pfrovide sufficient decorelation
                # between different paths if delay spread is sufficiently large.
                # Compute array covariance matrix R. Flatten CSI over horizontal and vertical dimensions of array.
                csi_flat = csi_combined.reshape(csi_combined.shape[0], csi_combined.shape[1], csi_combined.shape[2] * csi_combined.shape[3], csi_combined.shape[4])
                R = np.einsum("dbis,dbjs->ij", csi_flat, np.conj(csi_flat))
                self.beamspace_power = self._music_algorithm(R) if beamformer_type == "MUSIC" else self._mvdr_algorithm(R)

            # Option 2: Beamspace via FFT
            case "FFT":
                # For computational efficiency reasons, reduce number of datapoints to one by interpolating over all datapoints
                # This assumes a constant channel except for CFO-induced phase rotations and noise
                csi_combined = np.asarray([espargos.util.csi_interp_iterative(csi_combined, iterations = 5)])

                # Exploit time-domain sparsity to reduce number of 2D FFTs from antenna space to beamspace
                csi_tdomain = np.fft.ifftshift(np.fft.ifft(np.fft.fftshift(csi_combined, axes = -1), axis = -1), axes = -1)
                tap_count = csi_tdomain.shape[-1]
                csi_tdomain_cut = csi_tdomain[...,tap_count//2 + 1 - 16:tap_count//2 + 1 + 17]
                csi_fdomain_cut = np.fft.ifftshift(np.fft.fft(np.fft.fftshift(csi_tdomain_cut, axes = -1), axis = -1), axes = -1)

                # Here, we only go to DFT beamspace, not directly azimuth / elevation space,
                # but the shader can take care of fixing the distortion.
                # csi_zeropadded has shape (datapoints, azimuth / row, elevation / column, subcarriers)                
                csi_zeropadded = np.zeros((csi_fdomain_cut.shape[0], self.args.resolution_azimuth, self.args.resolution_elevation, csi_fdomain_cut.shape[-1]), dtype = csi_fdomain_cut.dtype)
                real_rows_half = csi_fdomain_cut.shape[2] // 2
                real_cols_half = csi_fdomain_cut.shape[3] // 2
                zeropadded_rows_half = csi_zeropadded.shape[2] // 2
                zeropadded_cols_half = csi_zeropadded.shape[1] // 2
                csi_zeropadded[:,zeropadded_cols_half-real_cols_half:zeropadded_cols_half+real_cols_half,zeropadded_rows_half-real_rows_half:zeropadded_rows_half+real_rows_half,:] = np.swapaxes(csi_fdomain_cut[:,0,:,:,:], 1, 2)
                csi_zeropadded = np.fft.ifftshift(csi_zeropadded, axes = (1, 2))
                beam_frequency_space = np.fft.fft2(csi_zeropadded, axes = (1, 2))
                beam_frequency_space = np.fft.fftshift(beam_frequency_space, axes = (1, 2))
                self.beamspace_power = np.sum(np.abs(beam_frequency_space)**2, axis = (0, 3))

            case "Bartlett":
                # For computational efficiency reasons, reduce number of datapoints to one by interpolating over all datapoints
                # This assumes a constant channel except for CFO-induced phase rotations and noise
                csi_combined = np.asarray([espargos.util.csi_interp_iterative(csi_combined, iterations = 5)])

                # Compute sum of received power per steering angle over all datapoints and subcarriers
                # real 2d spatial spectrum is too slow...
                # we can use 2D FFT to get to beamspace, which of course is technically not correct
                # (cannot separate 2D steering vector into Kronecker product of azimuth / elevation steering vectors)
                beam_frequency_space = np.einsum("rcae,dbrcs->daes", np.conj(self.steering_vectors_2d), csi_combined, optimize = True)
                self.beamspace_power = np.sum(np.abs(beam_frequency_space)**2, axis = (0, 3))

        if self.democonfig.get("visualization", "overlay") == "Power":
            db_beamspace = 10 * np.log10(self.beamspace_power)
            db_beamspace_norm = (db_beamspace - np.max(db_beamspace) + 15) / 15
            db_beamspace_norm = np.clip(db_beamspace_norm, 0, 1)
            color_beamspace = self._viridis(db_beamspace_norm)
        
            alpha_channel = np.ones((*color_beamspace.shape[:2], 1))
            color_beamspace_rgba = np.clip(np.concatenate((color_beamspace, alpha_channel), axis=-1), 0, 1)
            self.beamspace_power_imagedata = np.asarray(np.swapaxes(color_beamspace_rgba, 0, 1).ravel() * 255, dtype = np.uint8)
        else:
            power_visualization_beamspace = self.beamspace_power**3

            if self.args.manual_exposure:
                color_value = power_visualization_beamspace / (10 ** ((1 - self.exposure) / 0.1) + 1e-8)
            else:
                color_value = power_visualization_beamspace / (np.max(power_visualization_beamspace) + 1e-6)

            if self.democonfig.get("beamformer", "colorize_delay"):
                if self.democonfig.get("beamformer", "type") in ["MUSIC", "MVDR"]   :
                    raise NotImplementedError("Delay colorization not supported in MUSIC or MVDR mode")

                # Compute beam powers and delay. Beam power is value, delay is hue.
                beamspace_weighted_delay_phase = np.sum(beam_frequency_space[...,1:] * np.conj(beam_frequency_space[...,:-1]), axis=(0, 3))
                delay_by_beam = np.angle(beamspace_weighted_delay_phase)
                mean_delay = np.angle(np.sum(beamspace_weighted_delay_phase))

                hsv = np.zeros((beam_frequency_space.shape[1], beam_frequency_space.shape[2], 3))
                hsv[:,:,0] = (np.clip((delay_by_beam - mean_delay) / self.democonfig.get("beamformer", "max_delay"), 0, 1) + 1/3) % 1.0
                hsv[:,:,1] = 0.8
                hsv[:,:,2] = color_value

                wifi_image_rgb = matplotlib.colors.hsv_to_rgb(hsv)
                alpha_channel = np.ones((*wifi_image_rgb.shape[:2], 1))
                wifi_image_rgba = np.clip(np.concatenate((wifi_image_rgb, alpha_channel), axis=-1), 0, 1)
                self.beamspace_power_imagedata = np.asarray(np.swapaxes(wifi_image_rgba, 0, 1).ravel() * 255, dtype = np.uint8)
            else:
                self.beamspace_power_imagedata = np.zeros(4 * self.beamspace_power.size, dtype = np.uint8)
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
        Qn = eig_vec[:,order][:,1:]
        spatial_spectrum = 1 / np.linalg.norm(np.einsum("ae,a...->e...", Qn, np.conj(steering_vectors_2d_flat)), axis = 0)

        return spatial_spectrum - np.min(spatial_spectrum) + 1e-6

    def _mvdr_algorithm(self, R):
        # Compute spatial spectrum using MVDR algorithm based on R
        steering_vectors_2d_flat = self.steering_vectors_2d.reshape(-1, self.steering_vectors_2d.shape[2], self.steering_vectors_2d.shape[3])

        # MVDR is sensitive to ill-conditioned covariance; use diagonal loading and symmetrization
        R = (R + np.conj(R.T)) / 2
        loading = 0.1 * np.trace(R) / R.shape[0]
        R_loaded = R + loading * np.eye(R.shape[0])
        R_inv = np.linalg.pinv(R_loaded)
        denom = np.einsum("a...,ab,b...->...", np.conj(steering_vectors_2d_flat), R_inv, steering_vectors_2d_flat)
        spatial_spectrum = 1.0 / np.maximum(np.real(denom), 1e-12)

        return spatial_spectrum - np.min(spatial_spectrum) + 1e-6

    def _viridis(self, values):
        viridis_colormap = np.asarray([
            (0.267004, 0.004874, 0.329415),
            (0.229739, 0.322361, 0.545706),
            (0.127568, 0.566949, 0.550556),
            (0.369214, 0.788888, 0.382914),
            (0.993248, 0.906157, 0.143936),
            (0.993248, 0.906157, 0.143936)
        ])

        n = len(viridis_colormap) - 1
        idx = values * n
        low = np.floor(idx).astype(int)
        high = np.ceil(idx).astype(int)
        t = idx - low

        c0 = viridis_colormap[low]
        c1 = viridis_colormap[high]

        return c0 * (1 - t[:,:,np.newaxis]) + c1 * t[:,:,np.newaxis]

    def _cb_predicate(self, csi_completion_state, csi_age):
        timeout_condition = False
        if self.args.csi_completion_timeout > 0:
            timeout_condition = np.sum(csi_completion_state) >= 2 and csi_age > self.args.csi_completion_timeout

        return np.all(csi_completion_state) or timeout_condition

    def onAboutToQuit(self):
        self.videocamera.stop()
        super().onAboutToQuit()
        self.engine.deleteLater()

    @PyQt6.QtCore.pyqtSlot(dict)
    def onUpdateAppState(self, newcfg):
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

        if "visualization" in newcfg and "space" in newcfg["visualization"]:
            try:
                self.rawBeamspaceChanged.emit()
            except Exception as e:
                print(f"Error setting raw beamspace: {e}")

        # Let configmanager know we're done
        self.democonfig.updateAppStateHandled.emit()

    @PyQt6.QtCore.pyqtProperty(bool, constant=True)
    def music(self):
        return self.democonfig.get("beamformer", "type") == "MUSIC"

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def resolutionAzimuth(self):
        return self.args.resolution_azimuth

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def resolutionElevation(self):
        return self.args.resolution_elevation

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify = fovAzimuthChanged)
    def fovAzimuth(self):
        return self.democonfig.get("camera", "fov_azimuth")
    
    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify = fovElevationChanged)
    def fovElevation(self):
        return self.democonfig.get("camera", "fov_elevation")

    @PyQt6.QtCore.pyqtProperty(bool, constant=True)
    def manualExposure(self):
        return self.args.manual_exposure

    @PyQt6.QtCore.pyqtSlot(float)
    def adjustExposure(self, exposure):
        self.exposure = exposure

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify = rawBeamspaceChanged)
    def rawBeamspace(self):
        return self.democonfig.get("visualization", "space")

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify = rssiChanged)
    def rssi(self):
        return self.mean_rssi

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify = activeAntennasChanged)
    def activeAntennas(self):
        return self.mean_active_antennas
    
    @PyQt6.QtCore.pyqtProperty(bool, constant=True)
    def macListEnabled(self):
        return self.args.mac_list
    
    @PyQt6.QtCore.pyqtProperty(list, constant=False, notify = recentMacsChanged)
    def macList(self):
        return self.recent_macs
    
    @PyQt6.QtCore.pyqtSlot(str)
    def setMacFilter(self, mac):
        self.pool.set_mac_filter({"enable": True, "mac": mac})

    @PyQt6.QtCore.pyqtSlot()
    def clearMacFilter(self):
        self.pool.clear_mac_filter()

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify = cameraFlipChanged)
    def cameraFlip(self):
        return self.democonfig.get("camera", "flip")

app = EspargosDemoCamera(sys.argv)
sys.exit(app.exec())
