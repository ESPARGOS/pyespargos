import numpy as np

from demos.iq.camera.processing import NoiseFloor, detect_emissions, direction_grid, locate, peak_directions

HALF_WAVELENGTH_HZ = 299792458 / (2 * 0.06)


def plane_wave(u, v, rows=2, columns=4):
    return np.exp(1j * np.pi * (np.arange(columns)[np.newaxis, :] * u + np.arange(rows)[:, np.newaxis] * v)).ravel()


def test_noise_floor_estimates_mean_noise_power_despite_bursts():
    rng = np.random.default_rng(1)
    floor = NoiseFloor(antennas=8)
    for index in range(60):
        power = rng.gamma(8, 1 / 8, (56, 64))
        if index % 5 == 0:
            power[:20, 10:30] += 100.0  # an intermittent signal must not raise the floor
        floor.update(power, now=float(index))
    np.testing.assert_allclose(floor.power, 1.0, rtol=0.25)


def test_noise_floor_is_unavailable_until_enough_events_arrived():
    floor = NoiseFloor(antennas=8)
    floor.update(np.ones((4, 16)), now=0.0)
    assert floor.power is None


def test_emissions_at_different_times_are_located_separately():
    rng = np.random.default_rng(2)
    spectra = (rng.normal(size=(8, 40, 64)) + 1j * rng.normal(size=(8, 40, 64))) / np.sqrt(2)
    first, second = plane_wave(0.4, 0.0), plane_wave(-0.5, 0.1)
    spectra[:, 5:10, 20:30] += 30 * first[:, np.newaxis, np.newaxis] * np.exp(2j * np.pi * rng.random((5, 10)))
    spectra[:, 25:32, 40:44] += 30 * second[:, np.newaxis, np.newaxis] * np.exp(2j * np.pi * rng.random((7, 4)))
    emissions = detect_emissions(spectra, np.ones(64), 6.0, np.ones(64, dtype=bool))
    assert len(emissions) == 2
    assert np.flatnonzero(emissions[0].bins).tolist() == list(range(20, 30))
    assert np.flatnonzero(emissions[1].bins).tolist() == list(range(40, 44))
    steering = direction_grid(2, 4, HALF_WAVELENGTH_HZ, 64, 32)
    for emission, (u, v) in zip(emissions, ((0.4, 0.0), (-0.5, 0.1))):
        spectrum, purity = locate(emission.covariance, steering)
        assert purity > 0.95
        np.testing.assert_allclose(peak_directions(spectrum, 0.5)[0], (u, v), atol=0.02)


def test_disallowed_bins_and_noise_yield_no_emission():
    rng = np.random.default_rng(3)
    spectra = (rng.normal(size=(8, 40, 64)) + 1j * rng.normal(size=(8, 40, 64))) / np.sqrt(2)
    spectra[:, 5:10, 31:34] += 30.0
    allowed = np.ones(64, dtype=bool)
    allowed[30:35] = False
    assert detect_emissions(spectra, np.ones(64), 6.0, allowed) == []


def test_direction_grid_scales_with_frequency():
    steering = direction_grid(2, 4, 0.9 * HALF_WAVELENGTH_HZ, 64, 32)
    spectrum, _ = locate(np.outer(plane_wave(0.45, 0.0), np.conj(plane_wave(0.45, 0.0))), steering, "FFT")
    np.testing.assert_allclose(peak_directions(spectrum, 0.5)[0][0], 0.5, atol=0.02)


def test_incoherent_reception_has_low_purity():
    rng = np.random.default_rng(4)
    samples = rng.normal(size=(8, 400)) + 1j * rng.normal(size=(8, 400))
    _, purity = locate(samples @ np.conj(samples.T), direction_grid(2, 4, HALF_WAVELENGTH_HZ, 64, 32))
    assert purity < 0.3


def test_peaks_must_be_separated_in_azimuth():
    steering = direction_grid(2, 4, HALF_WAVELENGTH_HZ, 64, 32)
    image = sum(weight * locate(np.outer(plane_wave(u, v), np.conj(plane_wave(u, v))), steering)[0] for u, v, weight in ((0.3, -0.1, 1.0), (-0.5, 0.1, 0.5), (0.32, 0.6, 0.9)))
    directions = peak_directions(image, 0.1)
    assert len(directions) == 2
    np.testing.assert_allclose(directions, [(0.3, -0.1), (-0.5, 0.1)], atol=0.02)


def test_history_handles_bursts_motion_and_loss_of_signal():
    from demos.iq.camera.processing import DirectionHistory

    history = DirectionHistory()
    tau = 0.2
    left, right = np.eye(2, dtype=np.float32)
    for stamp, power in [(0.0, 1), (0.1, 20), (0.23, 0.02)]:
        history.add(power * left, stamp, tau)
        assert history.opacity(stamp + 0.2, tau) == 1
        np.testing.assert_array_equal(history.power, left)
    # Neither a late event nor an empty capture extends the lifetime.
    history.add(right, 0.17, tau)
    history.add(0 * right, 0.31, tau)
    assert history.received == 0.23
    for stamp in (0.33, 0.43, 0.53):
        history.add(right, stamp, tau)
    assert history.power.argmax() == 1
    assert 0 < history.opacity(1.13, tau) < 1
    assert history.opacity(1.24, tau) == 0
    history.add(left, 1.25, tau)
    np.testing.assert_array_equal(history.power, left)


def test_broad_single_lobe_generates_one_direction_label():
    u, v = np.mgrid[-1:1:128j, -1:1:64j]
    lobe = np.exp(-3 * (u**2 + v**2))
    directions = peak_directions(lobe, 0.1)
    assert len(directions) == 1
    np.testing.assert_allclose(directions[0], (0, 0), atol=0.01)


def test_short_capture_noise_floor_is_translation_invariant_across_nyquist():
    from demos.iq.camera.processing import spatial_noise_power

    rng = np.random.default_rng(507)
    for antennas in (8, 32):
        spectra = (rng.standard_normal((antennas, 1, 256)) + 1j * rng.standard_normal((antennas, 1, 256))).astype(np.complex64)
        floor = spatial_noise_power(spectra)
        shifted = spatial_noise_power(np.roll(spectra, 27, axis=-1))
        np.testing.assert_allclose(shifted, np.roll(floor, 27), rtol=1e-5)
        # Twenty-four snapshots cannot support an unrestricted 32-sensor
        # covariance. Independent unit-scale receiver noise must stay finite.
        assert np.median(floor) > 0.7


def test_fast_interval_stream_tracks_a_step_within_five_new_observations():
    from demos.iq.camera.processing import DirectionHistory

    left, right = np.eye(2, dtype=np.float32)
    history = DirectionHistory()
    for index in range(30):
        history.add(left, index * 0.013, 0.2)
    for index in range(30, 35):
        history.add(right, index * 0.013, 0.2)
    assert history.power.argmax() == 1
