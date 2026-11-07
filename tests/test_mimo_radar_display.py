import importlib.util
from pathlib import Path

import numpy as np

path = Path(__file__).resolve().parents[1] / "demos/iq-mimo-radar/mimo_processing.py"
spec = importlib.util.spec_from_file_location("mimo_radar_processing", path)
radar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radar)


def test_noise_reference_does_not_turn_up_the_brightness_of_a_quiet_frame():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    noise = np.ones((len(p.ranges), p.doppler_bins))
    angles = np.ones((len(p.ranges), len(p.angles)))
    quiet = p.display_maps((noise, angles))
    assert max(np.max(v) for v in quiet) == 0
    moving = noise.copy()
    moving[20, np.argmin(abs(p.velocities - 0.75))] = 100
    rd, _ = p.display_maps((moving, angles))
    assert abs(rd.max() - 20) < 1e-5
    assert abs(np.median(rd)) < 1e-5
    scaled = p.display_maps((moving * 10, angles * 10))
    np.testing.assert_allclose(rd, scaled[0])


def test_zero_input_produces_finite_dark_maps():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    for i in range(p.history):
        p.push(np.zeros((12, radar.SAMPLES), np.complex64), i * radar.INTERVAL)
    for values in p.display_maps(p.maps()):
        assert np.all(np.isfinite(values))
        assert values.max() <= 0


def test_leakage_alignment_brings_a_nearby_target_into_view():
    positions = np.array([[0.09, 0.03], [0.03, 0.03], [-0.03, 0.03], [-0.09, 0.03], [0.09, -0.03], [0.03, -0.03], [-0.03, -0.03], [-0.09, -0.03]])
    p = radar.MIMOProcessor(positions)
    mask = np.repeat(p.masks, 6, axis=0)
    reference = 20 * np.exp(-2j * np.pi * p.offsets * 2 * 150 / radar.C) * mask
    delay = 2 * 156 / radar.C - p.virtual_positions[:, 0, None] * np.sin(np.deg2rad(24)) / radar.C
    target = np.exp(-2j * np.pi * (p.frequency + p.offsets) * delay) * mask
    velocity = p.velocities[np.argmin(abs(p.velocities - 0.75))]
    from espargos.iq_timing import TXRXDelayCalibration
    from test_iq_timing import calibration

    cal = calibration()
    transmitter = cal.select(direction="tx").groups[0].corrections[0].state.antenna
    timing = TXRXDelayCalibration.from_reference(cal, measured_delay_s=2 * 150 / radar.C)
    correction = timing.correction(transmitter, p.offsets)
    for i in range(3 * p.history):
        p.push((reference + target * np.exp(-2j * np.pi * 2 * p.frequency * velocity / radar.C * i * p.dt)) * correction, i * radar.INTERVAL)
    rd, ra = p.maps()
    r, v = np.unravel_index(np.argmax(rd), rd.shape)
    assert abs(timing.delays_s[transmitter] * radar.C / 2 - 150) < 0.25
    assert abs(p.ranges[r] - 6) < 0.3
    assert abs(p.velocities[v] - velocity) < 0.01
    assert abs(p.angles[np.argmax(ra.max(axis=0))] - 24) <= 2


def test_stationary_coupling_with_gain_phase_and_timing_jitter_is_suppressed():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    reference = 100 * np.repeat(p.masks, 6, axis=0).astype(complex)
    for i in range(p.history):
        jitter = np.sin(2 * np.pi * 4 * i * p.dt)
        gain = (1 + 0.004 * jitter) * np.exp(0.003j * jitter)
        phase = np.exp(-2j * np.pi * p.offsets * 20e-12 * jitter)
        p.push(reference * gain * phase, i * radar.INTERVAL)
    rd, ra = p.maps()
    assert rd.max() < 0.01
    assert ra.max() < 0.01


def test_slow_target_survives_stationary_coupling_cancellation():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    reference = 100 * np.repeat(p.masks, 6, axis=0).astype(complex)
    target = reference * 0.01 * np.exp(-2j * np.pi * p.offsets * 2 * 5 / radar.C)
    velocity = p.velocities[np.argmin(abs(p.velocities - 0.3))]
    for i in range(p.history):
        moving = target * np.exp(-2j * np.pi * 2 * p.frequency * velocity / radar.C * i * p.dt)
        p.push(reference + moving, i * radar.INTERVAL)
    rd, ra = p.maps()
    r, v = np.unravel_index(rd.argmax(), rd.shape)
    assert abs(p.ranges[r] - 5) < 0.4
    assert p.velocities[v] == velocity
    assert rd.max() > 100
    assert ra.max() > 100


def test_point_target_uses_the_full_pilot_bandwidth():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    reference = 100 * np.repeat(p.masks, 6, axis=0).astype(complex)
    target = reference * 0.01 * np.exp(-2j * np.pi * p.offsets * 2 * 6 / radar.C)
    velocity = p.velocities[np.argmin(abs(p.velocities - 0.75))]
    for i in range(p.history):
        p.push(reference + target * np.exp(-2j * np.pi * 2 * p.frequency * velocity / radar.C * i * p.dt), i * radar.INTERVAL)
    rd, _ = p.maps()
    profile = rd[:, np.argmax(rd.max(axis=0))]
    above_half = p.ranges[profile > profile.max() / 2]
    assert abs(p.ranges[profile.argmax()] - 6) < 0.25
    assert 1.25 < above_half[-1] - above_half[0] < 2.0


def test_virtual_rows_do_not_cancel_at_nonzero_elevation():
    positions = np.array([[0.09, 0.03], [0.03, 0.03], [-0.03, 0.03], [-0.09, 0.03], [0.09, -0.03], [0.03, -0.03], [-0.03, -0.03], [-0.09, -0.03]])
    peaks = []
    for vertical_direction in (0, 0.5, -0.75):
        p = radar.MIMOProcessor(positions)
        assert p.aperture_shape == (3, 7)
        mask = np.repeat(p.masks, 6, axis=0)
        reference = 100 * mask.astype(complex)
        direction = np.array([np.sin(np.deg2rad(24)), vertical_direction])
        delay = (2 * 6 - p.virtual_positions @ direction) / radar.C
        target = np.exp(-2j * np.pi * (p.frequency + p.offsets) * delay[:, None]) * mask
        velocity = p.velocities[np.argmin(abs(p.velocities - 0.75))]
        for i in range(p.history):
            p.push(reference + target * np.exp(-2j * np.pi * 2 * p.frequency * velocity / radar.C * i * p.dt), i * radar.INTERVAL)
        _, ra = p.maps()
        r, a = np.unravel_index(ra.argmax(), ra.shape)
        assert abs(p.ranges[r] - 6) < 0.25
        assert abs(p.angles[a] - 24) <= 2
        half_power = p.angles[ra[r] > ra[r, a] / 2]
        assert half_power[-1] - half_power[0] <= 20
        peaks.append(ra[r, a])
    assert 10 * np.log10(max(peaks) / min(peaks)) < 0.2


def test_bounded_band_edge_correction_narrows_a_filtered_return():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    mask = np.repeat(p.masks, 6, axis=0)
    reference = mask / (1 + 4 * (p.offsets / (radar.RATE / 2)) ** 2)
    p.set_range_response(reference)
    assert np.min(p.range_weights) >= 1
    assert np.max(p.range_weights) <= 1.5
    ranges = np.linspace(-5, 5, 2001)
    delay = np.exp(2j * np.pi * p.offsets[:, None] * 2 * ranges / radar.C)
    widths = []
    for weights in (np.ones(radar.SAMPLES), p.range_weights):
        power = np.mean(abs((reference * weights) @ delay) ** 2, axis=0)
        above_half = ranges[power > power.max() / 2]
        widths.append(above_half[-1] - above_half[0])
    assert widths[1] < 0.95 * widths[0]
    p.set_range_response(mask.astype(complex))
    np.testing.assert_allclose(p.range_weights, 1)


def test_quantized_pilots_remain_orthogonal_and_have_equal_channel_gain():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    waveforms = [wave.synthesize(samples=radar.SAMPLES) for wave in radar.pilots()]
    responses = (0.7 * np.exp(0.3j), 0.4 * np.exp(-0.8j))
    for active in ((0,), (1,), (0, 1)):
        iq = np.broadcast_to(sum(responses[t] * waveforms[t] for t in active), (6, radar.SAMPLES))
        channels = p.channels(iq, np.ones_like(iq))
        for t in range(2):
            recovered = channels[t * 6 : (t + 1) * 6].sum(axis=-1) / (radar.SAMPLES / 2)
            np.testing.assert_allclose(recovered, responses[t] if t in active else 0, atol=1e-6)


def test_display_preserves_power_ratios_between_ranges():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    rd = np.ones((len(p.ranges), p.doppler_bins))
    rd[10] *= 10
    rd[80] *= 0.1
    ra = np.ones((len(p.ranges), len(p.angles)))
    ra[10] *= 10
    ra[80] *= 0.1
    displayed = p.display_maps((rd, ra))
    for image in displayed:
        np.testing.assert_allclose(image[10] - image[80], 20)


def test_pilots_alternate_across_every_fft_bin():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    masks = abs(p.pilots) > 0.1
    np.testing.assert_array_equal(masks.sum(axis=0), np.ones(radar.SAMPLES))
    np.testing.assert_array_equal(masks.sum(axis=1), [128, 128])
    order = np.argsort(p.offsets)
    np.testing.assert_array_equal(np.argmax(masks[:, order], axis=0), np.arange(radar.SAMPLES) % 2)
    for t in range(2):
        assert np.min(abs(p.pilots[t, masks[t]])) > 0.1
        assert np.max(abs(p.pilots[t, ~masks[t]])) < 1e-10


def test_receiver_dc_offsets_do_not_enter_radar_channels():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    rng = np.random.default_rng(10)
    iq = rng.normal(size=(6, radar.SAMPLES)) + 1j * rng.normal(size=(6, radar.SAMPLES))
    correction = np.exp(1j * rng.normal(size=iq.shape))
    expected = p.channels(iq, correction)
    for _ in range(10):
        offset = 100 * (rng.normal(size=(6, 1)) + 1j * rng.normal(size=(6, 1)))
        np.testing.assert_allclose(p.channels(iq + offset, correction), expected, atol=1e-6)
    assert not p.masks[:, 0].any()
    assert np.all(p.masks[:, 1:].sum(axis=0) == 1)


def test_angular_and_doppler_maps_preserve_the_same_doppler_peak():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    rng = np.random.default_rng(71)
    for i in range(p.history):
        samples = rng.normal(size=radar.SAMPLES) + 1j * rng.normal(size=radar.SAMPLES)
        # A disturbance near the end must receive the same window weight in both maps.
        if i > p.history - 8:
            samples *= 10
        p.push(np.broadcast_to(samples, (12, radar.SAMPLES)).astype(np.complex64), i * radar.INTERVAL)
    rd, ra = p.maps()
    # Co-located identical channels have unit coherent array gain after normalization.
    expected = rd.max(axis=1)
    np.testing.assert_allclose(ra, np.broadcast_to(expected[:, None], ra.shape), rtol=2e-5)


def test_combined_geometry_selects_only_global_corners():
    from types import SimpleNamespace
    from espargos import Pool
    from espargos.revisions import BoardRevisionDensiflorus

    config = Path(__file__).resolve().parents[1] / "config/aperture-kit-8x4.yml"
    pool = Pool.from_config(config)
    boards = [SimpleNamespace(revision=BoardRevisionDensiflorus()) for _ in pool.hosts]
    positions = pool._geometry.positions(boards).reshape(-1, 2)
    p = radar.MIMOProcessor(positions)
    assert p.receiver_count == 30
    assert p.path_count == 60
    np.testing.assert_allclose(positions[p.tx_indices[0]], positions.max(axis=0))
    np.testing.assert_allclose(positions[p.tx_indices[1]], positions.min(axis=0))
    assert len(set(p.rx_indices) | set(p.tx_indices)) == 32
    mask = np.repeat(p.masks, p.receiver_count, axis=0)
    delay = (2 * 5 - p.virtual_positions[:, 0] * np.sin(np.deg2rad(-30))) / radar.C
    target = np.exp(-2j * np.pi * (p.frequency + p.offsets) * delay[:, None]) * mask
    for i in range(p.history):
        p.push(100 * mask + target * np.exp(2j * np.pi * 5 * i / p.history), i * radar.INTERVAL)
    rd, ra = p.maps()
    r, a = np.unravel_index(ra.argmax(), ra.shape)
    assert abs(p.ranges[r] - 5) < 0.25
    assert abs(p.angles[a] + 30) <= 2
    assert rd.shape == (len(p.ranges), p.doppler_bins)


def test_radar_rejects_buffered_pre_start_windows_across_boards():
    import queue
    from types import SimpleNamespace

    import sys

    sys.path.insert(0, str(path.parent))
    import importlib
    import pytest

    pytest.importorskip("PyQt6")
    MIMORadar = importlib.import_module("iq-mimo-radar").MIMORadar
    s = SimpleNamespace()
    s._capturing = True
    s._frequency = 2385e6
    s._captures = queue.Queue()
    s._index = (np.array([0, 1]), np.array([0, 0]), np.array([0, 0]))
    s._epochs = np.array([123, 456], np.uint64)
    s._generations = np.array([10, 27], np.uint32)
    cluster = SimpleNamespace(
        vector_chunks=1,
        sample_rate_hz=radar.RATE,
        center_freq_hz=s._frequency,
        fire_time_ns=s._epochs.reshape(2, 1, 1),
        config_generation=np.array([9, 27]).reshape(2, 1, 1),
        counts=np.full((2, 1, 1, 1), 224),
        coverage_hashes=np.ones((2, 1, 1, 1)),
        flags=np.zeros((2, 1, 1, 1), dtype=int),
        iq=np.ones((2, 1, 1, 256), complex),
        source_chunk_start=1792,
    )
    MIMORadar._capture(s, cluster)
    assert s._captures.empty()
    cluster.config_generation[0, 0, 0] = 10
    MIMORadar._capture(s, cluster)
    assert s._captures.qsize() == 1 and s._coverage == 0.125


def test_each_virtual_row_contributes_to_angle_map():
    positions = np.array([(x * 0.06, y * 0.06) for y in range(2) for x in range(4)])
    p = radar.MIMOProcessor(positions)
    mask = np.repeat(p.masks, p.receiver_count, axis=0)
    for row in range(p.aperture_shape[0]):
        p.reset()
        target = mask * np.exp(-2j * np.pi * p.offsets * 12 / radar.C)
        target[p.aperture_y != row] = 0
        for i in range(p.history):
            p.push(100 * mask + target * np.exp(2j * np.pi * 5 * i / p.history), i * radar.INTERVAL)
        _, power = p.maps()
        r, a = np.unravel_index(power.argmax(), power.shape)
        assert power[r, a] > 1
        assert abs(p.ranges[r] - 6) < 0.25
        assert abs(p.angles[a]) <= 2


def test_correlation_beamspace_matches_full_spatial_fft():
    rng = np.random.default_rng(308)
    for rows, columns in ((2, 4), (4, 8), (2, 2)):
        positions = np.array([(x * 0.06, y * 0.06) for y in range(rows) for x in range(columns)])
        p = radar.MIMOProcessor(positions)
        p.ranges = p.ranges[:5]
        aperture = np.zeros((p.history, len(p.ranges), *p.aperture_shape), np.complex64)
        paths = (rng.normal(size=(*aperture.shape[:2], p.path_count)) + 1j * rng.normal(size=(*aperture.shape[:2], p.path_count))).astype(np.complex64)
        paths[::7] = 0
        for i, (x, y) in enumerate(zip(p.aperture_x, p.aperture_y)):
            aperture[:, :, y, x] += paths[..., i]
        beams = np.fft.fft2(aperture, s=p.spatial_fft_shape, axes=(-2, -1))
        expected = np.sum(abs(beams) ** 2, axis=-2) / p.spatial_fft_shape[0]
        np.testing.assert_allclose(p._beam_power(paths), expected, rtol=2e-5, atol=1e-5)


def test_weak_doppler_target_remains_visible_in_angle_map():
    positions = np.array([(x * 0.06, y * 0.06) for y in range(2) for x in range(4)])
    p = radar.MIMOProcessor(positions)
    rng = np.random.default_rng(45)
    mask = np.repeat(p.masks, p.receiver_count, axis=0)
    target = 0.08 * mask * np.exp(-2j * np.pi * p.offsets * 12 / radar.C)
    for i in range(p.history):
        noise = (rng.normal(size=mask.shape) + 1j * rng.normal(size=mask.shape)) * mask
        p.push(100 * mask + target * np.exp(2j * np.pi * 8 * i / p.history) + noise, i * radar.INTERVAL)
    rd, ra = p.display_maps(p.maps())
    r = np.argmin(abs(p.ranges - 6))
    assert rd[r].max() > 12
    assert ra[r, np.argmin(abs(p.angles))] > 12
    assert np.percentile(ra[-20:], 95) < 12


def test_every_tx_rx_path_reaches_both_maps():
    for rows, columns in ((2, 4), (4, 8), (2, 2)):
        positions = np.array([(x * 0.06, y * 0.06) for y in range(rows) for x in range(columns)])
        p = radar.MIMOProcessor(positions, history=8)
        mask = np.repeat(p.masks, p.receiver_count, axis=0)
        for path in range(p.path_count):
            p.reset()
            target = np.zeros_like(mask, dtype=complex)
            target[path] = mask[path] * np.exp(-2j * np.pi * p.offsets * 12 / radar.C)
            for i in range(p.history):
                p.push(100 * mask + target * np.exp(2j * np.pi * 2 * i / p.history), i * radar.INTERVAL)
            rd, ra = p.maps()
            assert rd.max() > 0
            assert ra.max() > 0
            assert abs(p.ranges[np.argmax(rd.max(axis=1))] - 6) < 0.25
            assert abs(p.ranges[np.argmax(ra.max(axis=1))] - 6) < 0.25


def test_pilots_emphasize_edges_without_raising_peak_or_crest_factor():
    p = radar.MIMOProcessor(np.zeros((8, 2)))
    weights = 1 + 0.5 * (2 * np.fft.fftfreq(radar.SAMPLES)) ** 2
    for t, waveform in enumerate(radar.pilots()):
        samples = waveform.synthesize(samples=radar.SAMPLES)
        assert np.max(abs(samples)) <= 1 + 1 / 511
        crest_db = 10 * np.log10(np.max(abs(samples) ** 2) / np.mean(abs(samples) ** 2))
        assert crest_db < 1.5
        active = abs(p.pilots[t]) > 0.1
        amplitude = abs(p.pilots[t, active])
        np.testing.assert_allclose(amplitude / amplitude.mean(), weights[active] / weights[active].mean(), atol=0.003)
        iq = np.broadcast_to(samples, (p.receiver_count, radar.SAMPLES))
        channels = p.channels(iq, np.ones_like(iq))
        recovered = abs(channels[t * p.receiver_count, p.masks[t]])
        expected = weights[p.masks[t]]
        np.testing.assert_allclose(recovered / recovered.mean(), expected / expected.mean(), atol=0.003)


def test_both_views_show_identical_peak_power_at_each_range():
    p = radar.MIMOProcessor(np.array([(x * 0.06, y * 0.06) for y in range(2) for x in range(4)]))
    rng = np.random.default_rng(189)
    for i in range(p.history):
        p.push((rng.normal(size=(p.path_count, radar.SAMPLES)) + 1j * rng.normal(size=(p.path_count, radar.SAMPLES))).astype(np.complex64), i * radar.INTERVAL)
    maps = p.maps()
    np.testing.assert_allclose(maps[0].max(axis=1), maps[1][:, p.visible_beams].max(axis=1), rtol=1e-6)
    rd, ra = p.display_maps(maps)
    np.testing.assert_allclose(rd.max(axis=1), ra[:, p.visible_beams].max(axis=1), atol=1e-5)


def test_dense_doppler_matches_full_zero_padded_beamforming():
    p = radar.MIMOProcessor(np.array([(x * 0.06, y * 0.06) for y in range(4) for x in range(8)]))
    assert p.history == 72
    assert p.doppler_bins == 288
    rng = np.random.default_rng(202)
    samples = (rng.normal(size=(p.history, 3, p.path_count)) + 1j * rng.normal(size=(p.history, 3, p.path_count))).astype(np.complex64)
    samples *= np.hanning(p.history).astype(np.float32)[:, None, None]
    coarse = radar.fft.fftshift(radar.fft.fft(samples, n=2 * p.history, axis=0), axes=0)
    dense = radar.fft.fftshift(radar.fft.fft(samples, n=p.doppler_bins, axis=0), axes=0)
    aperture = np.zeros((*dense.shape[:2], *p.aperture_shape), np.complex64)
    for path, (x, y) in enumerate(zip(p.aperture_x, p.aperture_y)):
        aperture[..., y, x] += dense[..., path]
    expected = np.sum(abs(radar.fft.fft(aperture, n=p.spatial_fft_shape[1], axis=-1)) ** 2, axis=-2)
    np.testing.assert_allclose(p._beam_power(coarse, p.doppler_bins), expected, rtol=2e-5, atol=0.03)


def test_cached_pilot_weights_preserve_channel_estimation():
    p = radar.MIMOProcessor(np.array([(x * 0.06, y * 0.06) for y in range(4) for x in range(8)]))
    rng = np.random.default_rng(31)
    iq = (rng.normal(size=(p.receiver_count, radar.SAMPLES)) + 1j * rng.normal(size=(p.receiver_count, radar.SAMPLES))).astype(np.complex64)
    correction = np.exp(1j * rng.uniform(-np.pi, np.pi, iq.shape))
    spectrum = np.fft.fft(iq, axis=-1) * correction
    expected = np.zeros((2, p.receiver_count, radar.SAMPLES), np.complex64)
    for t, mask in enumerate(p.masks):
        pilot = p.pilots[t, mask]
        template = pilot / abs(pilot) * np.mean(abs(pilot))
        expected[t][:, mask] = spectrum[:, mask] / template
        expected[t] *= (radar.SAMPLES / 2) / np.count_nonzero(mask)
    actual = p.channels(iq, correction)
    np.testing.assert_allclose(actual, expected.reshape(actual.shape), rtol=2e-5, atol=1e-6)


def test_radar_shutdown_closes_pool_even_if_callback_removal_fails():
    import importlib
    import sys
    from types import SimpleNamespace
    import pytest

    pytest.importorskip("PyQt6")
    sys.path.insert(0, str(path.parent))
    app = importlib.import_module("iq-mimo-radar").MIMORadar
    calls = []

    def remove_callback(callback):
        calls.append(("remove", callback))
        raise RuntimeError("callback cleanup failed")

    pool = SimpleNamespace(iq=SimpleNamespace(remove_accumulation_callback=remove_callback), close=lambda: calls.append("close"))
    state = SimpleNamespace(_pool=pool, _callback=7, _capturing=True)
    with pytest.raises(RuntimeError, match="callback cleanup"):
        app._close_radar(state)
    assert calls == [("remove", 7), "close"]
    assert state._pool is None and not state._capturing
    app._close_radar(state)
    assert calls == [("remove", 7), "close"]
