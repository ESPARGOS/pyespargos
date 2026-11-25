"""Check spatial training against known transmit-to-receive channel coefficients."""

import struct
from pathlib import Path

import numpy as np
import pytest

from espargos import WiFiIQPacket


@pytest.fixture
def generators(monkeypatch):
    tools = Path(__file__).resolve().parents[1] / "external/espargos-packet-decoders/tools"
    monkeypatch.syspath_prepend(str(tools))
    import wifi_he
    import wifi_ppdu

    return wifi_ppdu, wifi_he


def candidate(wave):
    # Leave headroom for acquisition, preserve native ADC polarity, and pad to
    # the capture word boundary. The PHY decoder must locate training itself.
    wave = np.pad(wave, (80, (-len(wave)) % 8 + 48))
    scale = 220 / np.max(abs(wave))
    raw = np.round(scale * wave.real).astype("i4") & 1023
    raw |= (np.round(-scale * wave.imag).astype("i4") & 1023) << 10
    header = bytearray(136)
    header[:4] = b"WFC2"
    struct.pack_into("<I", header, 44, 3)
    struct.pack_into("<2I", header, 56, len(raw), len(raw))
    struct.pack_into("<III", header, 72, 20000000, 2462000000, 1 << 18)
    struct.pack_into("<6H", header, 84, 65535, 0, 65535, len(raw), 0, 0)
    header[96] = 255
    header[-16] = 30
    return WiFiIQPacket(bytes(header) + raw.astype("<u4").tobytes())


@pytest.mark.parametrize("count,cp", [(2, 0), (3, 1), (4, 2), (5, 3), (6, 3), (7, 4), (8, 3)])
def test_he_separates_every_trained_channel(generators, count, cp):
    _, he = generators
    streams, _, _ = he.stimulus(streams=count, cp=cp, symbols=0)
    channel = np.exp(0.31j * np.arange(count)) / np.arange(1, count + 1)
    packet = candidate(streams @ channel)
    measurement = packet.spatial_csi
    assert measurement.spatial_streams == count
    assert measurement.phy == "HE20"
    np.testing.assert_allclose(measurement.coefficients / measurement.coefficients[0], np.broadcast_to(channel[:, None], measurement.coefficients.shape), atol=0.045)


def test_ht_spatial_mapping_includes_cyclic_shift(generators):
    ht, _ = generators
    h = 0.15 - 0.7j
    packet = candidate(ht.synthesize_ht20_mcs8(bytes(range(96)), h1=1, h2=h))
    measurement = packet.spatial_csi
    assert measurement.spatial_streams == 2
    assert measurement.coefficients.shape == (2, 56)
    tones = measurement.frequencies_hz // 312500
    expected = h * ht.HT_STBC_STREAM2_CSD[tones + 32]
    np.testing.assert_allclose(measurement.coefficients[1] / measurement.coefficients[0], expected, atol=0.035)
    assert packet.source_mac is None


def test_he_stream_channels_with_carrier_and_adc_offsets(generators):
    _, he = generators
    streams, _, _ = he.stimulus(streams=3, cp=2, symbols=1)
    channel = np.array([1, 0.6j, -0.4 + 0.6j])
    carrier = np.exp(2j * np.pi * 40_000 * np.arange(len(streams)) / 20_000_000)
    packet = candidate((streams @ channel) * carrier + 0.1 + 0.08j)
    measured = packet.spatial_csi.coefficients
    common = channel.conj() @ measured / np.vdot(channel, channel)
    residual = measured - channel[:, None] * common
    assert np.linalg.norm(residual) < 0.05 * np.linalg.norm(measured)


def test_shared_edge_matches_two_streams_despite_clock_map_offset(generators):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from espargos import CSIBacklog, SensorMessage, WiFiIQReceiver
    from espargos.revisions import BoardRevisionDensiflorus

    ht, _ = generators
    board = SimpleNamespace(revision=BoardRevisionDensiflorus(), wifi_iq=Mock())
    receiver = WiFiIQReceiver(SimpleNamespace(boards=[board]))
    backlog = CSIBacklog(receiver, fields=["spatial_csi"], apply_calibration=False, callback_predicate=lambda c: c.completion.sum() == 2)
    for antenna, h in enumerate([1j, -0.25j]):
        p = candidate(ht.synthesize_ht20_mcs8(bytes(range(64)), h2=h))
        # Both instants on the second antenna have the same UART clock offset.
        p.boot_us = 10_000 + antenna * 105
        p.time_us = p.boot_us + 60
        receiver._input_queue.put_nowait((0, SensorMessage(0, antenna, p)))
    receiver._run(0)
    measurement = backlog.get_latest("spatial_csi").item()
    assert measurement.coefficients.shape == (1, 2, 4, 2, 56)
    h = measurement.coefficients.reshape(8, 2, 56)
    complete = np.isfinite(h).all(axis=(1, 2))
    assert complete.sum() == 2
    assert np.isnan(h[~complete]).all()
    assert np.isfinite(measurement.rx_gain).sum() == 2
    assert np.linalg.matrix_rank(h[complete, :, 20]) == 2


def test_independent_identical_phy_headers_need_a_common_adc_time(generators):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from espargos import SensorMessage, WiFiIQReceiver
    from espargos.revisions import BoardRevisionDensiflorus

    ht, _ = generators
    receiver = WiFiIQReceiver(SimpleNamespace(boards=[SimpleNamespace(revision=BoardRevisionDensiflorus(), wifi_iq=Mock())]))
    wave = ht.synthesize_ht20_mcs8(bytes(range(64)), h2=0.5j)
    for antenna in range(2):
        p = candidate(wave)
        p.evidence &= ~(1 << 18)
        p.time_us = 10_000
        receiver._add_sensor_message(0, SensorMessage(0, antenna, p))
    clusters = receiver._cluster_caches["packets"].values()
    assert [c.completion.sum() for c in clusters] == [1, 1]


@pytest.mark.parametrize("offset, expected_gain", [(440, 34), (480, None), (536, None)])
def test_ht_gain_relative_to_spatial_training(generators, offset, expected_gain):
    ht, _ = generators
    packet = candidate(ht.synthesize_ht20_mcs8(bytes(range(96)), h2=0.45j))
    start = packet.phy_header["training_start"]
    change = (34 << 13) | (start + offset)
    packet._gain_trace = ((change << 7) | 30).to_bytes(16, "little")
    result = packet.spatial_csi
    if expected_gain is None:
        assert result is None
    else:
        assert result.rx_gain == expected_gain
        assert result.coefficients.shape == (2, 56)


def test_he_rejects_gain_transition_within_ltf(generators):
    _, he = generators
    streams, _, _ = he.stimulus(streams=2, cp=2)
    p = candidate(streams @ np.array([1, 0.5j]))
    assert p.phy_header is not None
    start = p.phy_header["training_start"] + 528 + 32
    entry = (31 << 13) | (start + 5)
    p._gain_trace = ((entry << 7) | 30).to_bytes(16, "little")
    assert p.spatial_csi is None


def test_adc_positions_distinguish_consecutive_identical_phy_headers():
    from espargos.wifi_iq.receiver import _WindowTiming

    timing = _WindowTiming((1, 2, 4))
    a, b = (0, 0, 0), (0, 0, 1)
    first = (a, 7, 16382), (b, 3, 98)
    assert timing.compare_packet(*first) is None
    timing.compare_packet(*first, learn=True)
    assert timing.compare_packet(*first) is None
    timing.compare_packet((b, 3, 502), (a, 7, 401), learn=True)
    assert timing.compare_packet(*first)
    assert not timing.compare_packet(first[0], (b, 3, 98 + 4096))
    assert timing.compare_packet(first[0], (b, 4, 98)) is None


def test_he_constellation_recovers_both_payload_streams(generators):
    from espargos.wifi_iq.symbols import symbol_constellation

    _, he = generators
    tx, expected, _ = he.stimulus(streams=2, cp=3, symbols=2)
    packets = [candidate(tx @ h) for h in ([1, 0.25j], [0.15j, 1])]
    for packet in packets:
        packet.phy = packet.phy_header["phy"]
    data = np.asarray(symbol_constellation(packets)["Data"]).reshape(expected.shape)
    assert np.linalg.norm(data - expected) / np.linalg.norm(expected) < 0.05
    assert not symbol_constellation([packets[0], packets[0]])["Data"]
