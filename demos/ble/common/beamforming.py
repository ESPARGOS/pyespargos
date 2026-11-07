"""Camera presentation of library BLE direction estimates."""

import numpy as np
from espargos.ble_beamforming import BLEBeamformingConfig
from espargos.polarization import polarization_ellipse


def processing_config(config):
    return BLEBeamformingConfig(
        method=config["type"], history_size=int(config["backlog_size"]), history_age=float(config["backlog_age"]), polarization=config.get("polarization_mode", "ignore") != "ignore", multipath=bool(config.get("multipath", True))
    )


def render_spectrum(result, shape, width=0.025):
    """Draw fitted paths as finite-width blobs for the camera overlay."""
    if result.spectrum is not None:
        return result.spectrum
    u = (2 * np.arange(shape[0]) + 1) / shape[0] - 1
    v = (2 * np.arange(shape[1]) + 1) / shape[1] - 1
    power = np.zeros(shape)
    for (x, y), weight in zip(result.directions, result.strengths / max(np.max(result.strengths, initial=0), 1e-30)):
        power += weight * np.exp(-((u[:, None] - x) ** 2 + (v[None, :] - y) ** 2) / (2 * width**2))
    return power


def render_polarization(result, shape, width=0.025):
    """Show a resolved path's fitted polarization, not its Bartlett sidelobes."""
    if result.path_jones is None:
        return result.jones
    u = (2*np.arange(shape[0])+1)/shape[0]-1
    v = (2*np.arange(shape[1])+1)/shape[1]-1
    coherency = np.zeros((*shape,2,2),complex)
    for (x,y), strength, jones in zip(result.directions,result.strengths,result.path_jones):
        weight = strength*np.exp(-((u[:,None]-x)**2+(v[None,:]-y)**2)/(2*width**2))
        coherency += weight[...,None,None]*np.outer(jones,jones.conj())
    return polarization_ellipse(coherency)


def measurement_opacity(age, max_age):
    """Keep fresh measurements steady, then fade only when updates stop.

    The short grace period absorbs packet/GUI scheduling jitter without averaging
    measurements or delaying replacement by a newer result.
    """
    grace = min(.2, max_age/2)
    return float(np.clip((max_age-age)/(max_age-grace),0,1))


def spectrum_intensity(power):
    """WiFi-style automatic exposure before applying measurement freshness."""
    intensity = np.maximum(power,0)**3
    return intensity / max(float(np.max(intensity)),1e-30)
