"""Camera presentation of library BLE direction estimates."""

import numpy as np
from espargos.ble_beamforming import BLEBeamformingConfig, spectrum_peaks
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
    u = (2 * np.arange(shape[0]) + 1) / shape[0] - 1
    v = (2 * np.arange(shape[1]) + 1) / shape[1] - 1
    coherency = np.zeros((*shape, 2, 2), complex)
    for (x, y), strength, jones in zip(result.directions, result.strengths, result.path_jones):
        weight = strength * np.exp(-((u[:, None] - x) ** 2 + (v[None, :] - y) ** 2) / (2 * width**2))
        coherency += weight[..., None, None] * np.outer(jones, jones.conj())
    return polarization_ellipse(coherency)


def measurement_opacity(age, max_age):
    """Keep fresh measurements steady, then fade only when updates stop.

    The short grace period absorbs packet/GUI scheduling jitter without averaging
    measurements or delaying replacement by a newer result.
    """
    grace = min(0.2, max_age / 2)
    return float(np.clip((max_age - age) / (max_age - grace), 0, 1))


def spectrum_intensity(power):
    """WiFi-style automatic exposure before applying measurement freshness."""
    intensity = np.maximum(power, 0) ** 3
    return intensity / max(float(np.max(intensity)), 1e-30)


def combine_board_spectra(records, now, max_age, *, bartlett=True):
    """Fuse one advertiser's board-local powers, then expose and locate it once."""
    power = np.zeros(records[0][1].shape)
    freshness = 0.0
    consensus = bartlett and len(records) > 1
    total_weight = 0.0
    for received, spectrum, _ in records:
        opacity = measurement_opacity(now - received, max_age)
        if consensus:
            # The direct path should be supported across the aperture. A
            # geometric mean limits the influence of a single board's sidelobes.
            peak = float(spectrum.max())
            if peak <= 0:
                continue
            power += opacity * np.log(np.maximum(spectrum / peak, 0.01))
            total_weight += opacity
        else:
            power += opacity * spectrum
        freshness = max(freshness, opacity)
    if not freshness:
        return np.zeros_like(power), []
    if consensus:
        power = np.exp(power / total_weight)
    if len(records) == 1 and records[0][2]:
        directions = records[0][2]
    else:
        u, v = ((2 * np.arange(size) + 1) / size - 1 for size in power.shape)
        physical = np.where(u[:, None] ** 2 + v**2 <= 1, power, -np.inf)
        count = max(1, max(len(paths) for _, _, paths in records))
        if count == 1:
            # Broadside can lie midway between adjacent pixels. A whole
            # ridge of tied peaks (one measured baseline) has no unique angle.
            a, e = np.where(np.isclose(physical, physical.max(), rtol=1e-8, atol=0))
            directions = [(u[a].mean(), v[e].mean())] if a.size and np.ptp(a) <= 1 and np.ptp(e) <= 1 else []
        else:
            directions = [(u[a], v[e]) for a, e in spectrum_peaks(physical, count)]
    return spectrum_intensity(power) * freshness, directions
