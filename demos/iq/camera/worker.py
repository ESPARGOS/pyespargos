"""Persistent camera DSP, isolated from packet acquisition and Qt's interpreter.

The process boundary carries only arrays and plain configuration. There are no
board connections, Qt objects or callbacks in the processor's state.
"""

import time
import numpy as np
from demos.iq.camera.processing import NoiseFloor, channelize, detect_emissions, direction_grid, locate, flash_image, spatial_noise_power
from demos.iq.common.iq_waterfall import power_db_row


class CaptureProcessor:
    def __init__(self):
        self.noise_key = None
        self.noise = None

    def process(self, samples, correction, window, shape, center, sample_rate, noise_key, beamformer, visualization, now):
        started = time.perf_counter()
        bins = samples.shape[-1]
        frequencies = center + np.fft.fftshift(np.fft.fftfreq(bins, d=1 / sample_rate))
        spectra = channelize(samples, window, beamformer["reject_dc"]) * correction
        power = np.mean(np.abs(spectra) ** 2, axis=0)
        rows = power_db_row(10 * np.log10(np.maximum(power, 1e-12)), bins)
        grid = direction_grid(*shape, center, beamformer["resolution_azimuth"], beamformer["resolution_elevation"])
        image = np.zeros(grid.shape[:2], dtype=np.float32)
        strongest = None
        if noise_key != self.noise_key:
            self.noise = NoiseFloor(spectra.shape[0])
            self.noise_key = noise_key
        if self.noise.needs_update(now):
            self.noise.update(power, now, spatial_noise=spatial_noise_power(spectra))
        if self.noise.power is not None:
            allowed = np.ones(bins, dtype=bool)
            if beamformer["reject_dc"]:
                allowed[bins // 2 - 2 : bins // 2 + 3] = False
            if beamformer["band_limit"]:
                allowed &= (frequencies >= beamformer["band_low_mhz"] * 1e6) & (frequencies <= beamformer["band_high_mhz"] * 1e6)
            emissions = detect_emissions(spectra, self.noise.power, beamformer["threshold_db"], allowed, power=power)
            min_bins = beamformer["min_bandwidth_mhz"] * 1e6 * bins / sample_rate
            for emission in emissions:
                if np.ptp(np.flatnonzero(emission.bins)) + 1 < min_bins:
                    continue
                spectrum, purity = locate(emission.covariance, grid, beamformer["type"], snapshots=emission.snapshots, dynamic_range_db=beamformer.get("source_dynamic_range_db", 12.0))
                if purity < beamformer["min_purity"]:
                    continue
                # A reflected plane wave can also have high purity. This
                # weights fit quality, not confidence in line of sight.
                weight = emission.power * purity**2
                mode = visualization["display_mode"]
                if mode in ("flashes", "azimuth"):
                    spectrum = flash_image(spectrum, visualization["flash_width_deg"], azimuth_only=mode == "azimuth")
                np.maximum(image, weight * spectrum, out=image)
                if strongest is None or weight > strongest[0]:
                    occupied = frequencies[emission.bins] / 1e6
                    strongest = (weight, emission.power, occupied[0], occupied[-1], purity)
        return dict(rows=rows, image=image, strongest=strongest, noise=self.noise.power, span=(frequencies[0] / 1e6, frequencies[-1] / 1e6), dsp_ms=(time.perf_counter() - started) * 1000)


_processor = None


def process_capture(*args):
    global _processor
    if _processor is None:
        _processor = CaptureProcessor()
    return _processor.process(*args)


def initialize_processor():
    """Start the worker during receiver initialization, before captures arrive."""
    global _processor
    if _processor is None:
        _processor = CaptureProcessor()
