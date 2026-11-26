"""Exercise the packet-IQ wire adapter and CSI backlog with a recorded WiFi waveform."""

import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from espargos import CSIBacklog, MACFilter, SensorMessage, WiFiIQPacket, WiFiIQReceiver
from espargos.revisions import BoardRevisionDensiflorus

FIXTURE = Path(__file__).resolve().parents[1] / "external/espargos-packet-decoders/tests/fixtures/wifi_ofdm_native.npz"
MAC = bytes.fromhex("024553504951")


def record(kind, time_us=10000, *, missing=0):
    with np.load(FIXTURE) as fixture:
        words = fixture["native_ofdm_6"] if kind != b"WFM1" else np.array([], np.uint32)
    header = bytearray(160 if kind.startswith(b"WFN") else 136)
    header[:4] = kind
    struct.pack_into("<QQQ", header, 8, 8000, time_us, time_us)
    struct.pack_into("<I", header, 44, 2)
    struct.pack_into("<II", header, 56, len(words), len(words))
    candidate = kind.startswith(b"WFC")
    evidence = 1 << 18 if candidate else (1 << 18) | 15
    struct.pack_into("<III", header, 72, 20000000, 2432000000, evidence)
    struct.pack_into("<6H", header, 84, 65535 if candidate else 194, 0, 1200, 2048, 68, 32)
    header[96] = 255 if candidate else 0
    header[98] = 2
    if not candidate:
        header[104:110] = MAC
        header[110] = 24
        header[112] = 8
    if len(words):
        struct.pack_into("<Q", header, len(header) - 24, missing)
        header[-16] = 30
    return bytes(header) + words.astype("<u4").tobytes()


@pytest.mark.parametrize("split", ["native", "metadata first", "waveform first"])
def test_csi_backlog_from_two_receivers(split):
    board = SimpleNamespace(revision=BoardRevisionDensiflorus(), wifi_iq=Mock())
    receiver = WiFiIQReceiver(SimpleNamespace(boards=[board]))
    expected = receiver._expected[0]
    expected[:] = False
    for antenna in (0, 7):
        expected[board.revision.antenna_id_to_row_col(antenna)] = True
    backlog = CSIBacklog(receiver, fields=["lltf", "mac", "rf_switch_state"], apply_calibration=False)
    backlog.add_filter(MACFilter(MAC.hex()))
    for antenna in (0, 7):
        tags = [b"WFN2"] if split == "native" else [b"WFM1", b"WFC2"]
        if split == "waveform first":
            tags.reverse()
        for tag in tags:
            p = WiFiIQPacket(record(tag, 10000 + antenna * 5))
            p.rf_switch = 2 if antenna == 0 else 3
            receiver._input_queue.put_nowait((0, SensorMessage(1, antenna, p)))
    receiver._run(0)
    channels, mac, feeds = backlog.get_multiple(["lltf", "mac", "rf_switch_state"])
    for antenna, route in ((0, 2), (7, 3)):
        row, col = board.revision.antenna_id_to_row_col(antenna)
        assert feeds[0, 0, row, col] == route
    assert channels.shape == (1, 1, 2, 4, 53)
    assert mac.tobytes() == MAC
    assert np.isfinite(channels[0, 0, expected]).all()
    assert np.isnan(channels[0, 0, ~expected]).all()
    np.testing.assert_allclose(*channels[0, 0, expected])


def test_delayed_datagram_after_reboot_and_generation_wrap():
    board = SimpleNamespace(revision=BoardRevisionDensiflorus(), wifi_iq=Mock())
    receiver = WiFiIQReceiver(SimpleNamespace(boards=[board]))

    def deliver(clock, generation, time_us):
        p = WiFiIQPacket(record(b"WFN2", time_us))
        p.clock_epoch = clock
        p.generation = generation
        return receiver._add_sensor_message(0, SensorMessage(1, 0, p))

    assert deliver(501, 0xFFFFFFFF, 10000)
    assert deliver(501, 0, 20000)
    assert deliver(501, 0xFFFFFFFF, 30000) is None
    board.wifi_iq.set_config.return_value = {"clock_epoch": 812, "generation": 1}
    receiver._apply_config(0, {"rx_gain": 35})
    assert deliver(501, 2, 40000) is None
    assert deliver(812, 1, 50000)


@pytest.mark.parametrize("changed", ["generation", "clock_epoch", "receiver_epoch"])
def test_stream_reports_invalidate_calibration(changed):
    from espargos.wifi_iq.receiver import _PhaseCorrection

    board = SimpleNamespace(revision=BoardRevisionDensiflorus(), wifi_iq=Mock())
    receiver = WiFiIQReceiver(SimpleNamespace(boards=[board]))
    first = WiFiIQPacket(record(b"WFN2"))
    _, _, cluster = receiver._add_sensor_message(0, SensorMessage(1, 0, first))
    calibration = _PhaseCorrection(cluster, np.where(cluster.completion[..., None], 1 + 0j, np.nan))
    calibration.generation = first.generation
    receiver._corrections[0] = calibration
    assert receiver.has_calibration()
    second = WiFiIQPacket(record(b"WFN2", 30000))
    setattr(second, changed, getattr(first, changed) + 1)
    update = receiver._add_sensor_message(0, SensorMessage(1, 0, second))
    receiver._on_cluster_updated(*update)
    assert not receiver.has_calibration()


def test_reference_sequence_groups_reports_despite_uart_clock_offset():
    from dataclasses import replace
    from espargos.wifi_iq.receiver import _Frame

    board = SimpleNamespace(revision=BoardRevisionDensiflorus(), wifi_iq=Mock())
    receiver = WiFiIQReceiver(SimpleNamespace(boards=[board]))
    frame = _Frame(0, 1, 2432000000, 4, 1, MAC.hex(), 32, 36, bytes(24))
    first = frame, 10000, None, 0
    later = frame, 10750, None, 7
    resolve = lambda key: receiver._resolve_cluster_key("packets", key, {first: None}, 0, None)
    assert resolve(later) == later
    receiver._calibration_reference = 0, frame.generation, frame.source_mac
    assert resolve(later) == first
    next_packet = replace(frame, sequence_control=48), 10750, None, 7
    assert resolve(next_packet) == next_packet


def test_training_crossing_missing_chunk_is_excluded():
    assert WiFiIQPacket(record(b"WFN2", missing=1 << 7)).ofdm is None
    with pytest.raises(ValueError):
        WiFiIQPacket(record(b"WFN2")[:-1])


def test_calibrated_channel_tracks_gain_changes_and_excludes_unknown_gains():
    from espargos.gain_phase_calibration import GAIN_PHASE_SHIFT_BY_GAIN_RAD
    from espargos.wifi_iq.receiver import _PhaseCorrection

    frame = SimpleNamespace(clock_epoch=1, frequency=2432000000, generation=4, rf_switch=2)
    cluster = SimpleNamespace(frame=frame, timing=None, epochs=np.ones(3), rx_gain=np.full(3, 30.0), completion=np.ones(3, bool))
    shifts = np.array(GAIN_PHASE_SHIFT_BY_GAIN_RAD)
    correction = _PhaseCorrection(cluster, np.full((3, 1), np.exp(-1j * shifts[30])))
    correction.generation = frame.generation
    cluster.rx_gain = np.array([24, 44, np.nan])
    channel = np.exp(1j * shifts[[24, 44, 30]])[:, None]
    measured = correction.for_cluster(cluster).apply_lltf(channel)
    np.testing.assert_allclose(measured[:2], 1, atol=1e-6)
    assert np.isnan(measured[2]).all()
    frame.frequency += 5000000
    assert correction.for_cluster(cluster) is None


def test_fft_window_timing_preserves_channel_delay_and_missing_antennas():
    from espargos.wifi_iq.receiver import _WindowTiming, _WINDOW_ROTATION

    timing = _WindowTiming((3,))
    # Derive counter origins from a common packet near the ring wrap. The next
    # packet starts 10 samples later and its local FFT starts differ by +1/-1.
    np.testing.assert_array_equal(timing.shifts(np.array([62, 19, 35]), np.ones(3)), 0)
    shifts = timing.shifts(np.array([8, 30, 44]), np.ones(3))
    tones = np.arange(-26, 27)
    # A new propagation delay on antenna 1 is independent of its FFT start.
    channel = np.exp(-2j * np.pi / 64 * np.outer([0, 3.25, 0.4], tones))
    measured = channel * np.exp(2j * np.pi / 64 * np.outer([0, 1, -1], tones))
    np.testing.assert_allclose(measured * _WINDOW_ROTATION[shifts], channel, atol=1e-6)

    # With the first antenna missing the common FFT origin can move, while
    # the measured transfer function between the remaining antennas stays put.
    shifts = timing.shifts(np.array([-1, 30, 44]), np.ones(3))
    aligned = measured * _WINDOW_ROTATION[shifts]
    np.testing.assert_allclose(aligned[2] / aligned[1], channel[2] / channel[1], atol=1e-6)


def test_fft_window_timing_links_disjoint_partial_packets():
    from espargos.wifi_iq.receiver import _WindowTiming

    clock = _WindowTiming((4,))
    for positions in ([10, 15, -1, -1], [-1, -1, 50, 47], [-1, 25, 33, -1]):
        clock.shifts(np.array(positions), np.ones(4))
    # Groups 0/1 and 2/3 first overlapped on antennas 1 and 2.
    np.testing.assert_array_equal(clock.shifts(np.array([61, 2, 11, 7]), np.ones(4)), [0, 0, 1, 0])


def test_estimator_uses_selected_fields_and_skips_missing_samples():
    from espargos.wifi_iq.estimator import refine_channels

    packet = WiFiIQPacket(record(b"WFN2"))
    packet.half_rate = 12
    packets = dict.fromkeys(range(3), packet)
    training = np.stack([packet.ofdm[3]] * 3)
    headers = refine_channels(packets, "headers", training)
    payload = refine_channels(packets, "payload", training)
    assert np.max(abs(headers - training)) > 1
    assert np.max(abs(payload - headers)) > 1
    packet.invalid_chunks = (1 << 64) - 1
    np.testing.assert_allclose(refine_channels(packets, "payload", training), training, rtol=1e-6)


def test_symbol_constellation_contains_measured_data_and_drops_missing_payload():
    from espargos.wifi_iq.symbols import symbol_constellation

    packet = WiFiIQPacket(record(b"WFN2"))
    packet.half_rate = 12
    points = symbol_constellation([packet])
    assert len(points["Preamble"]) == 104 and len(points["L-SIG"]) == 48
    assert points["Pilots"] and not points["HT-SIG"]
    data = np.asarray(points["Data"])
    assert data.size == 384
    error = np.sqrt(np.mean(np.minimum(abs(data - 1), abs(data + 1)) ** 2))
    assert 0.001 < error < 0.3
    first_data_chunk = (packet.ofdm[0] + 224) // 32
    packet.invalid_chunks = ((1 << 64) - 1) ^ ((1 << first_data_chunk) - 1)
    truncated = symbol_constellation([packet])
    assert truncated["Preamble"] and not truncated["Data"]


def test_barker_channel_and_symbols_from_recorded_cck_packet():
    from espargos.packet_decoders import wifi
    from espargos.wifi_iq.symbols import symbol_constellation

    with np.load(FIXTURE.with_name("wifi_cck_iq.npz")) as fixture:
        raw = fixture["native_rate7"]
    decoded = wifi(raw)
    board = SimpleNamespace(revision=BoardRevisionDensiflorus(), wifi_iq=Mock())
    rx = WiFiIQReceiver(SimpleNamespace(boards=[board]))
    backlog = CSIBacklog(rx, fields=["lltf", "dsss", "iq_packets"], apply_calibration=False, callback_predicate=lambda c: c.completion.sum() == 2)
    # Remove one SYNC symbol from the second antenna's training window and
    # rotate its entire waveform by 180 degrees. Both shifts must survive
    # association without introducing a phase slope or a polarity ambiguity.
    for antenna in (0, 1):
        p = WiFiIQPacket(record(b"WFN2"))
        p.raw = raw if not antenna else ((-raw & 1023) | ((-(raw >> 10) & 1023) << 10))
        p.phy = 1
        p.training = decoded["training_start"] + antenna * 20
        p.training_words = decoded["training_words"] - antenna * 20
        p.cfo = decoded["cfo_hz"]
        rx._input_queue.put_nowait((0, SensorMessage(0, antenna, p)))
    rx._run(0)
    assert np.isnan(backlog.get_latest("lltf")).all()
    a, b = (backlog.get_latest("dsss")[(0, *board.revision.antenna_id_to_row_col(i))] for i in (0, 1))
    assert a.shape == (35,) and np.isfinite(a).all()
    np.testing.assert_allclose(b, -a, atol=0.05 * np.max(abs(a)))
    symbols = symbol_constellation(backlog.get_latest("iq_packets"))
    assert len(symbols["Preamble"]) > 10 and len(symbols["Data"]) > 100
    assert not symbols["Pilots"]


def test_rejoining_receiver_learns_a_new_ring_origin():
    from espargos.wifi_iq.receiver import _WindowTiming

    clock = _WindowTiming((3,), period=256)
    clock.shifts(np.array([100, 120, 140]), np.array([1, 1, 1]))
    shifts = clock.shifts(np.array([150, 171, 63]), np.array([1, 1, 2]))
    np.testing.assert_array_equal(shifts, [0, 1, 0])
    shifts = clock.shifts(np.array([200, 221, 115]), np.array([1, 1, 2]))
    np.testing.assert_array_equal(shifts, [0, 1, 2])


def test_first_preamble_can_already_have_different_fft_starts():
    from espargos.wifi_iq.receiver import _WindowTiming

    clock = _WindowTiming((2,), period=256)
    tones = np.arange(-26, 27)
    for offset, path_delay in [(5, 0.2), (-2, 3.2)]:
        channel = np.array([np.ones(53), np.exp(2j * np.pi * tones * (offset - path_delay) / 64)])
        shifts = clock.shifts(np.array([850, 1200 + offset]), np.ones(2), channel)
        assert (shifts[1] - shifts[0] - offset) % 256 == 0
