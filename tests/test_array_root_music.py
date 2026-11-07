import numpy as np
from espargos.array_processing import root_music_2d, bartlett_fft_2d, mdl_source_count, steering_vectors_2d

F = 299792458 / (2 * 0.06)


def manifold(rows, cols, directions):
    r, c = np.indices((rows, cols))
    return np.exp(1j * np.pi * (c.ravel()[:, None] * np.array(directions)[:, 0] + r.ravel()[:, None] * np.array(directions)[:, 1]))


def test_single_and_paired_sources_on_both_array_sizes():
    for rows, cols in [(2, 4), (6, 4), (4, 6)]:
        for directions in [[(0.23, -0.31)], [(-0.4, 0.2), (0.35, -0.25)]]:
            a = manifold(rows, cols, directions)
            R = (a * np.arange(1, len(directions) + 1)) @ a.conj().T + 0.01 * np.eye(rows * cols)
            got, powers, purity = root_music_2d(R, rows, cols, frequency_hz=F, snapshots=1000)
            assert len(got) == len(directions)
            np.testing.assert_allclose(sorted(map(tuple, got)), sorted(directions), atol=0.002)
            np.testing.assert_allclose(powers, np.arange(1, len(directions) + 1)[::-1], rtol=0.02)


def test_white_noise_has_no_mdl_sources():
    assert mdl_source_count(np.ones(8), 1000) == 0
    got, _, purity = root_music_2d(np.eye(8), 2, 4, frequency_hz=F)
    assert not len(got) and purity == 0


def test_bartlett_fft_matches_direct_quadratic_form():
    rng = np.random.default_rng(71)
    z = rng.normal(size=(8, 40)) + 1j * rng.normal(size=(8, 40))
    R = z @ z.conj().T
    for freq in [F, 0.9 * F]:
        a = steering_vectors_2d(2, 4, 64, 32, frequency_hz=freq, pixel_centers=True).reshape(8, 64, 32)
        expected = np.einsum("auv,ab,buv->uv", a.conj(), R, a).real
        u = (2 * np.arange(64) + 1) / 64 - 1
        v = (2 * np.arange(32) + 1) / 32 - 1
        expected[u[:, None] ** 2 + v[None, :] ** 2 > 1] = 0
        got = bartlett_fft_2d(R, 2, 4, 64, 32, frequency_hz=freq)
        np.testing.assert_allclose(got, expected, atol=0.003 * expected.max())


def test_finite_noisy_snapshots_and_azimuth_elevation_pairing():
    from tools.benchmark_array_processing import recording, angular_error, FREQUENCY

    rng = np.random.default_rng(2026)
    for shape in [(2, 4), (6, 4)]:
        for snr in [0, 10, 20]:
            for count in [1, 2]:
                for _ in range(10):
                    truth = np.array([[-0.4, 0.2], [0.35, -0.25]])[:count] + rng.uniform(-0.08, 0.08, (count, 2))
                    x = recording(rng, shape, truth, snr)
                    covariance = x @ x.conj().T / x.shape[1]
                    got, _, _ = root_music_2d(covariance, *shape, frequency_hz=FREQUENCY, snapshots=x.shape[1])
                    assert len(got) == count
                    assert angular_error(got, truth) < 3


def test_coherent_paths_have_one_observable_mode():
    from tools.benchmark_array_processing import recording, FREQUENCY

    rng = np.random.default_rng(7)
    x = recording(rng, (2, 4), [(-0.4, 0.2), (0.35, -0.25)], 20, coherent=True)
    covariance = x @ x.conj().T / x.shape[1]
    assert mdl_source_count(np.linalg.eigvalsh(covariance), x.shape[1]) == 1
    got, _, purity = root_music_2d(covariance, 2, 4, frequency_hz=FREQUENCY, snapshots=x.shape[1])
    assert len(got) <= 1
    assert purity < 0.8


def test_broadside_and_negative_axes():
    for direction in [(0, 0), (0, -0.3), (-0.3, 0), (0.7, -0.5)]:
        a = manifold(2, 4, [direction])
        got, _, _ = root_music_2d(a @ a.conj().T + 0.01 * np.eye(8), 2, 4, frequency_hz=F)
        np.testing.assert_allclose(got, [direction], atol=1e-5)


def test_fft_peak_on_large_array_and_non_half_wave_spacing():
    frequency = 0.9 * F
    a = manifold(6, 4, [(0.9 * 0.25, 0.9 * -0.3)])
    spectrum = bartlett_fft_2d(a @ a.conj().T, 6, 4, 128, 64, frequency_hz=frequency)
    i, j = np.unravel_index(np.argmax(spectrum), spectrum.shape)
    np.testing.assert_allclose(((2 * i + 1) / 128 - 1, (2 * j + 1) / 64 - 1), (0.25, -0.3), atol=0.02)
