import struct
import zlib
from types import SimpleNamespace

import numpy as np
import pytest

from espargos.iq_receiver import IQReceiver
from espargos.iq_rx_stream import IQStreamBlock, IQStreamPacket
from espargos.calibration import CalibrationState


def record(generation=7, grid=3, fire=110000000):
    # Provide a wire fixture independent of the parser, including a 64-bit index.
    header = struct.pack("<4sIIIQQQI", b"IRS1", generation, grid, 2437000000, 2**32 + 128, fire, 100000000, 16000)
    iq = np.tile(np.array([32, -64], dtype="<i2"), 128)
    body = header + iq.tobytes()
    return body + struct.pack("<I", zlib.crc32(body))


def test_stream_wire_and_corruption():
    data = record()
    p = IQStreamPacket(data)
    assert p.first_sample == 2**32 + 128
    unaligned = bytearray(data[:-4])
    struct.pack_into("<Q", unaligned, 16, p.first_sample + 1)
    with pytest.raises(ValueError, match="header"):
        IQStreamPacket(unaligned + struct.pack("<I", zlib.crc32(unaligned)))
    np.testing.assert_array_equal(p.decode_iq(), np.full(128, 1 - 2j))
    damaged = bytearray(data)
    damaged[45] ^= 1
    for malformed in (damaged, data[1:], data + b"\x00"):
        with pytest.raises(ValueError):
            IQStreamPacket(malformed)


def test_array_alignment_requires_a_common_grid():
    receiver = SimpleNamespace(boards=[None, None])

    def key(board, antenna, p):
        return IQReceiver._get_cluster_key(receiver, board, SimpleNamespace(payload=p, antenna_id=antenna))

    a = IQStreamPacket(record())
    assert key(0, 0, a) != key(0, 1, a)
    assert key(0, 0, a) != key(0, 0, IQStreamPacket(record(fire=42)))
    grid = (1 << 18) | (1 << 16) | 7
    a = IQStreamPacket(record(grid=grid))
    b = IQStreamPacket(record(generation=5, grid=grid, fire=450000000))
    assert key(0, 0, a) == key(1, 7, b)
    assert key(0, 0, a) != key(1, 7, IQStreamPacket(record(grid=grid + 1)))
    revision = SimpleNamespace(antenna_id_to_row_col=lambda n: divmod(n, 4))
    cluster = IQStreamBlock(a, [revision, revision])
    for board, antenna, packet in ((0, 0, a), (1, 7, b)):
        cluster.add_message(board, SimpleNamespace(payload=packet, antenna_id=antenna))
    # A tail from the previous settings can arrive on the same sample grid.
    # The other board has its own counter, but each board must be homogeneous.
    assert not cluster.add_message(0, SimpleNamespace(payload=b, antenna_id=1))
    assert not cluster.add_message(1, SimpleNamespace(payload=a, antenna_id=6))
    assert cluster.synchronized and cluster.completion.sum() == 2
    assert np.isnan(cluster.iq[~cluster.completion]).all()
    assert cluster.fire_time_ns[1, 1, 3] == b.fire_time_ns


def test_free_running_antennas_deliver_independent_partial_blocks_immediately():
    receiver = IQReceiver(SimpleNamespace(boards=[]), chunk_settle_timeout=60)
    receiver.board_revisions = (SimpleNamespace(antenna_id_to_row_col=lambda n: divmod(n, 4)),)
    receiver._expected = np.ones((1, 2, 4), dtype=bool)
    partial, complete = [], []
    receiver.add_stream_callback(partial.append, include_partial=True)
    receiver.add_stream_callback(complete.append)
    packet = IQStreamPacket(record(grid=0))
    for antenna in [3, 5]:
        receiver._input_queue.put_nowait((0, SimpleNamespace(antenna_id=antenna, payload=packet)))
    receiver._run(timeout=0)
    assert not complete
    assert [np.flatnonzero(block.completion).tolist() for block in partial] == [[3], [5]]
    assert not any(receiver._cluster_caches.values())
    receiver._close()


def test_delayed_reports_do_not_redeliver_an_array_block():
    receiver = IQReceiver(SimpleNamespace(boards=[]), chunk_settle_timeout=0)
    receiver.board_revisions = (SimpleNamespace(antenna_id_to_row_col=lambda n: divmod(n, 4)),)
    receiver._expected = np.ones((1, 2, 4), dtype=bool)
    received = []
    receiver.add_stream_callback(received.append, include_partial=True)
    packet = IQStreamPacket(record(grid=(1 << 18) | (1 << 16) | 7))
    reports = [(0, SimpleNamespace(antenna_id=a, payload=packet)) for a in range(8)]
    for batch in (reports, reports[:3], reports):
        for message in batch:
            receiver._input_queue.put_nowait(message)
        receiver._run(timeout=0)
    assert len(received) == 1
    assert received[0].is_complete


def test_calibration_follows_each_present_antenna_on_a_partial_grid():
    revision = SimpleNamespace(antenna_id_to_row_col=lambda n: divmod(n, 4))
    packet = IQStreamPacket(record(grid=(1 << 18) | (1 << 16) | 7))
    block = IQStreamBlock(packet, [revision])
    epochs = np.full(block.shape, packet.fire_time_ns, dtype=np.uint64)
    model = SimpleNamespace(phase_offsets=np.zeros(block.shape))
    receiver = SimpleNamespace(_calibration=model, _calibration_epoch=epochs.tobytes(), _calibration_sample_rate=16000, _calibration_center_frequency=2437000000)
    assert not IQReceiver.calibration_applies_to(receiver, block)
    block.add_message(0, SimpleNamespace(antenna_id=2, payload=packet))
    assert IQReceiver.calibration_applies_to(receiver, block)
    state = CalibrationState.from_iq_packet("array", 2, packet, boot_id=4)
    assert (state.epoch, state.sample_origin_ns) == (packet.fire_time_ns, packet.grid_origin_ns)
    model.phase_offsets[0, 0, 2] = np.nan
    assert not IQReceiver.calibration_applies_to(receiver, block)
    model.phase_offsets.fill(0)
    block.fire_time_ns[0, 0, 2] += 1
    assert not IQReceiver.calibration_applies_to(receiver, block)
    with pytest.raises(ValueError, match="synchronized"):
        CalibrationState.from_iq_packet("array", 2, IQStreamPacket(record()), boot_id=4)


def test_narrowband_noise_phase_fit():
    from espargos._iq_calibration.stream import _fit_noise

    rng = np.random.default_rng(81)
    excitation = rng.normal(size=(192, 128)) + 1j * rng.normal(size=(192, 128))
    delay = np.array([1.75, 0, -2.35])
    phase = np.array([2.4, 0, -1.9])
    response = np.exp(1j * (phase[:, None] + 2 * np.pi * delay[:, None] * np.fft.fftfreq(128)))
    samples = np.fft.ifft(np.fft.fft(excitation)[:, None, :] * response)
    measured_delay, measured_phase = _fit_noise(samples, reference=1)
    np.testing.assert_allclose(measured_delay, delay, atol=0.01)
    np.testing.assert_allclose(measured_phase, phase, atol=0.01)
    with pytest.raises(RuntimeError, match="bandwidth"):
        _fit_noise(np.zeros_like(samples), reference=1)


def test_raw_bluetooth_noise_fit_uses_the_analog_bandwidth():
    from espargos._iq_calibration.stream import _fit_noise

    rng = np.random.default_rng(810)
    n = 256
    f = np.fft.fftfreq(n)
    bandwidth = 1.2 / 32
    source = rng.normal(size=(256, n)) + 1j * rng.normal(size=(256, n))
    spectrum = np.fft.fft(source) * (abs(f) < 0.48 * bandwidth)
    # A narrow analog filter can delay the raw samples by more than four
    # ADC clocks while still moving the waveform only a fraction of its width.
    phase, delay = 0.67, 8.25
    samples = np.stack([np.fft.ifft(spectrum), np.fft.ifft(spectrum * np.exp(1j * (phase + 2 * np.pi * delay * f)))], axis=1)
    measured_delay, measured_phase = _fit_noise(samples, 0, bandwidth)
    np.testing.assert_allclose(measured_delay, [0, delay], atol=0.15)
    np.testing.assert_allclose(measured_phase, [0, phase], atol=0.015)


def test_applied_generation_waits_for_each_selected_receiver():
    from unittest.mock import Mock, patch
    from espargos.board_iq import IQCapability

    board = Mock(api_version=(5, 0))
    iq = IQCapability(board)
    board.control.post_json.return_value = '{"config_generation":12}'

    def state(last):
        return dict(config_generation=12, receiver_mask=5, applied_generations=[12, None, last])

    board.control.get_json.side_effect = [state(11), state(12)]
    with patch("espargos.board_iq.time.sleep") as sleep:
        assert iq.set_config_and_wait({"dc_tracking": False}) == 12
        sleep.assert_called_once()
    board.control.post_json.assert_called_once_with("set_iq_control", {"dc_tracking": False, "return_generation": True})
    board.control.get_json.side_effect = [dict(config_generation=13)]
    with pytest.raises(RuntimeError, match="superseded"):
        iq.set_config_and_wait({})
    board.control.get_json.side_effect = [state(None)]
    with patch("espargos.board_iq.time.monotonic", side_effect=[0, 2]):
        with pytest.raises(TimeoutError, match=r"sensors \[2\]"):
            iq.set_config_and_wait({}, timeout=1)
    board.control.post_json.return_value = "ok"
    with pytest.raises(RuntimeError, match="does not support"):
        iq.set_config_and_wait({})


@pytest.mark.parametrize("per_sensor_rate", [True, False])
def test_raw_calibration_tracks_filter_and_frequency_of_present_antennas(per_sensor_rate):
    from espargos.calibration import Antenna

    shape = (1, 2, 4)
    model = SimpleNamespace(phase_offsets=np.zeros(shape))
    fire = np.arange(8, dtype=np.uint64).reshape(shape) + 1
    receiver = SimpleNamespace(
        _owner=SimpleNamespace(hosts=["test"]),
        _calibration=model,
        _calibration_epoch=fire.tobytes(),
        _calibration_sample_rate=3_200_000,
        _calibration_center_frequency=2_440_000_000,
        boards=[SimpleNamespace(revision=SimpleNamespace(antenna_id_to_row_col=lambda a: divmod(a, 4)))],
    )
    source = SimpleNamespace(fire_time_ns=fire, completion=np.ones(shape, bool), center_freq_hz=2_440_000_000, sample_rate_hz=3_200_000)
    if per_sensor_rate:
        source.sample_rate_hz = np.full(shape, source.sample_rate_hz)
        source.center_freq_hz = np.full(shape, source.center_freq_hz)
    assert IQReceiver.calibration_applies_to(receiver, source)
    IQReceiver._invalidate_rx_phases(receiver, {Antenna("test", 5)})
    assert not IQReceiver.calibration_applies_to(receiver, source)
    source.completion[0, 1, 1] = False
    assert IQReceiver.calibration_applies_to(receiver, source)
    source.center_freq_hz += 1_000_000
    assert not IQReceiver.calibration_applies_to(receiver, source)
    source.center_freq_hz -= 1_000_000
    source.sample_rate_hz *= 2
    assert not IQReceiver.calibration_applies_to(receiver, source)


def test_pilot_ignores_other_grid_even_when_generation_matches():
    from espargos._iq_calibration.stream import _observe

    flags = (1 << 18) | (1 << 16)
    revision = SimpleNamespace(antenna_id_to_row_col=lambda a: divmod(a, 4))
    mask = np.ones((1, 2, 4), dtype=bool)
    frames = []
    for seq in [6, 7]:
        packet = IQStreamPacket(record(grid=flags | seq, fire=100 + seq))
        block = IQStreamBlock(packet, [revision])
        for a in range(8):
            block.add_message(0, SimpleNamespace(antenna_id=a, payload=packet))
        frames.append(block)
    board = SimpleNamespace(iq=SimpleNamespace(get_config=lambda: dict(config_generation=7, sync_seq=65543)))

    def subscribe(callback, callback_predicate):
        for block in frames:
            assert callback_predicate(block)
            callback(block)
        return callback

    receiver = SimpleNamespace(boards=[board], add_cluster_callback=subscribe, remove_callback=lambda h: None)
    _, epoch, _ = _observe(receiver, SimpleNamespace(active=[0], mask=mask), 2437000000, 16000, 0.1, 1, lambda x: x, "test")
    assert (epoch == 107).all()


def test_pilot_rejects_clipping_before_frequency_estimation(monkeypatch):
    from espargos._iq_calibration import stream

    mask = np.ones((1, 2, 4), dtype=bool)
    grid = np.ones(mask.shape, dtype=np.uint64)
    scope = SimpleNamespace(mask=mask, pairs=[(0, 0)])
    board = SimpleNamespace(revision=SimpleNamespace(antenna_id_to_row_col=lambda a: (0, a)), iq=SimpleNamespace(set_reference=lambda *args: None))
    receiver = SimpleNamespace(boards=[board], master_board=board)
    fs = 3_200_000
    offset = 203_000
    n = 256
    iq = np.tile(100 * np.exp(2j * np.pi * np.arange(n) * offset / fs), (8, 1))

    def observe(receiver, scope, center, rate, timeout, count, extract, description):
        clipped = iq.copy()
        clipped[3, 50] = 511 + 10j
        assert extract(clipped) is None
        return [extract(iq)] * count, grid, grid

    monkeypatch.setattr(stream, "_observe", observe)
    frequency, phase, _, _ = stream._tone(receiver, scope, 2440e6, offset, fs, 80, 3)
    assert abs(frequency - offset) < fs / n * 0.03
    np.testing.assert_allclose(phase, 0, atol=1e-7)


@pytest.mark.parametrize("rejection", [400, 409, 500, None])
def test_partial_path_change_preserves_only_definitively_rejected_boards(rejection, request):
    from unittest.mock import Mock
    from espargos.board import EspargosHTTPStatusError
    from espargos.calibration import Antenna

    boards = [Mock(host=h) for h in ["first", "second"]]
    for board in boards:
        board.iq.get_config.return_value = dict(modes=["iq_rx"] * 8, capture_path="bluetooth", bluetooth_filter_khz=1580)
        board.revision.antenna_id_to_row_col.side_effect = lambda a: divmod(a, 4)
    boards[1].iq.set_config.side_effect = EspargosHTTPStatusError(rejection) if rejection else TimeoutError("response lost")
    models = Mock()
    receiver = IQReceiver(SimpleNamespace(boards=boards, hosts=[b.host for b in boards], calibrations=models))
    request.addfinalizer(receiver._close)
    receiver._calibration = SimpleNamespace(phase_offsets=np.zeros((2, 2, 4)))
    with pytest.raises(RuntimeError, match="second"):
        receiver.apply_config({"bluetooth_filter_khz": 8300})
    models.without.assert_not_called()
    assert np.isfinite(receiver._calibration.phase_offsets).all()
    with pytest.raises(RuntimeError, match="second"):
        receiver.apply_config({"capture_path": "wifi"})
    changed_hosts = ["first"] if rejection in (400, 409) else ["first", "second"]
    models.without.assert_called_once_with({Antenna(host, a) for host in changed_hosts for a in range(8)})
    assert np.isnan(receiver._calibration.phase_offsets[0]).all()
    assert np.isnan(receiver._calibration.phase_offsets[1]).all() == ("second" in changed_hosts)


@pytest.mark.parametrize("attempt,backoff,spacing", [(0, 80, 3125000), (1, 80, 4000000), (2, 40, 3125000)])
@pytest.mark.parametrize("center,direction", [(2310000000, 1), (2640000000, -1)])
def test_narrow_high_gain_reference_at_tuning_limits(monkeypatch, attempt, backoff, spacing, center, direction):
    from unittest.mock import Mock
    from espargos._iq_calibration import stream

    mask = np.ones((1, 1, 2), dtype=bool)
    scope = SimpleNamespace(active=[0], ids={0: [0, 1]}, pairs=[(0, 0), (0, 1)], mask=mask)
    iq = Mock()
    board = SimpleNamespace(iq=iq, revision=SimpleNamespace(antenna_id_to_row_col=lambda antenna: (0, antenna)))
    receiver = SimpleNamespace(boards=[board], master_board=board)
    gains = [dict(antid=a, gain_mode="manual", rx_gain=g) for a, g in enumerate([46, 65])]
    configs = [dict(trigger=dict(mode="downsample"), bluetooth_filter_khz=1200, receivers=gains)]
    random = np.random.default_rng(54)
    epoch = np.ones(mask.shape, dtype=np.uint64)
    phase = 0.81

    def observe(receiver, scope, center, rate, timeout, count, extract, description):
        tone = random.normal(size=(count, 128)) + 1j * random.normal(size=(count, 128))
        return np.stack([tone, tone * np.exp(1j * phase)], axis=1), epoch, epoch

    monkeypatch.setattr(stream, "_observe", observe)
    _, measured, _, _ = stream.measure_bluetooth(receiver, scope, configs, center, 16000, 3, attempt)
    np.testing.assert_allclose(measured.ravel(), [0, phase], atol=1e-7)
    iq.set_config.assert_called_once_with({"receivers": gains})
    expected = [center + direction * offset for offset in [spacing, round(1.5 * spacing / 1000) * 1000]]
    assert [call.args for call in iq.set_reference.call_args_list] == [("iq", frequency, backoff) for frequency in expected]

    # A null between two usable observations must keep the first fit.
    # The next independent observation still measures the same receiver.
    fit = stream._fit_noise
    observed = []

    def null_on_second(*args):
        observed.append(phase)
        if len(observed) == 2:
            raise RuntimeError("Insufficient coherent bandwidth")
        return fit(*args)

    monkeypatch.setattr(stream, "_fit_noise", null_on_second)
    _, recovered, _, _ = stream.measure_bluetooth(receiver, scope, configs, center, 16000, 3, attempt)
    np.testing.assert_allclose(recovered, measured, atol=1e-7)
    assert len(observed) == 3
    monkeypatch.setattr(stream, "_fit_noise", fit)

    # Keep both training waveforms individually coherent, but change the
    # phase on the independent measurement. The two fits must disagree.
    def disagree(*args):
        nonlocal phase
        phase += 0.4
        return observe(*args)

    monkeypatch.setattr(stream, "_observe", disagree)
    with pytest.raises(RuntimeError, match="validation residual"):
        stream.measure_bluetooth(receiver, scope, configs, center, 16000, 3, attempt)
