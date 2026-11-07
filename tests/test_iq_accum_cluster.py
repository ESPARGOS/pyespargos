from types import SimpleNamespace

import numpy as np

from espargos.iq_accum_cluster import IQAccumCluster
from espargos.iq_cluster import IQCluster
from espargos.iq_receiver import IQReceiver


class _Revision:
    @staticmethod
    def antenna_id_to_row_col(antenna_id):
        return divmod(antenna_id, 4)


def _packet(lane, antenna_id=0, count=7, coverage_hash=None):
    value = 10 * antenna_id + lane
    if coverage_hash is None:
        coverage_hash = 1000 + lane
    return SimpleNamespace(
        fire_time_ns=1000000,
        window_index=9,
        source_chunk_start=4096,
        source_chunk_end=4224,
        vector_chunks=2,
        vector_chunk_index=lane,
        nominal_chunks=64,
        accumulated_chunks=count,
        coverage_hash=coverage_hash,
        deadline_aborts=0,
        adc_decimation=1,
        sample_rate_hz=80_000_000,
        center_freq_hz=2_437_000_000,
        rx_gain=60,
        gain_mode=1,
        config_generation=5,
        flags=1,
        decode_iq=lambda: np.full(256, value + 1j * value, dtype=np.complex64),
        sample_rx_gain=lambda: np.full(256, 60, dtype=np.uint8),
    )


def _message(antenna_id, packet):
    return SimpleNamespace(antenna_id=antenna_id, payload=packet)


def test_cluster_orders_sections_and_tracks_sensor_completion():
    cluster = IQAccumCluster(_packet(0), [_Revision()])
    cluster.add_message(0, _message(0, _packet(1)))
    assert not cluster.completion[0, 0, 0]
    cluster.add_message(0, _message(0, _packet(0)))
    assert cluster.completion[0, 0, 0]
    np.testing.assert_array_equal(cluster.iq[0, 0, 0, :256], 0)
    np.testing.assert_array_equal(cluster.iq[0, 0, 0, 256:], 1 + 1j)


def test_complete_cluster_requires_identical_coverage_across_sensors():
    cluster = IQAccumCluster(_packet(0), [_Revision()])
    for antenna_id in range(8):
        for lane in range(2):
            packet = _packet(lane, antenna_id)
            if antenna_id == 7 and lane == 1:
                packet.coverage_hash += 1
            cluster.add_message(0, _message(antenna_id, packet))
    assert cluster.is_complete
    assert not cluster.coverage_consistent


def test_raw_and_accumulation_callbacks_do_not_block_each_other():
    pool = IQReceiver(SimpleNamespace(boards=[]))
    pool._expected = np.ones((1, 2, 4), dtype=bool)
    seen = []
    pool.add_callback(lambda cluster: seen.append(("raw", cluster)))
    pool.add_accumulation_callback(lambda cluster: seen.append(("accum", cluster)))

    raw = IQCluster(1, [_Revision()], gain_phase_compensation=False)
    raw._completion[:] = True
    assert pool._try_callbacks(raw)

    accum = IQAccumCluster(_packet(0), [_Revision()])
    accum._completion[:] = True
    accum._counts[:] = 7
    accum._coverage_hashes[:] = np.asarray([1000, 1001], dtype=np.uint32)
    assert pool._try_callbacks(accum)
    assert [kind for kind, _cluster in seen] == ["raw", "accum"]


def test_accumulation_callback_accepts_only_the_configured_receivers():
    receiver = IQReceiver(SimpleNamespace(boards=[]))
    receiver._expected = np.ones((1, 2, 4), dtype=bool)
    receiver._expected[0, 0, 3] = False
    receiver._expected[0, 1, 0] = False
    seen = []
    receiver.add_accumulation_callback(seen.append)
    cluster = IQAccumCluster(_packet(0), [_Revision()])
    for antid in (0, 1, 2, 5, 6, 7):
        for lane in range(2):
            cluster.add_message(0, _message(antid, _packet(lane, antid)))
    assert not cluster.is_complete
    assert receiver._on_cluster_updated("accumulations", 1, cluster)
    assert seen == [cluster]


def test_mixed_accumulation_rejects_mismatched_coverage():
    receiver = IQReceiver(SimpleNamespace(boards=[]))
    receiver._expected = np.zeros((1, 2, 4), dtype=bool)
    receiver._expected[0, 0, :2] = True
    seen = []
    receiver.add_accumulation_callback(seen.append)
    cluster = IQAccumCluster(_packet(0), [_Revision()])
    for antid in (0, 1):
        for lane in range(2):
            cluster.add_message(0, _message(antid, _packet(lane, antid, coverage_hash=antid)))
    assert not receiver._try_callbacks(cluster)
    assert not seen


def test_combined_accumulation_accepts_controller_local_generations():
    cluster = IQAccumCluster(_packet(0), [_Revision(), _Revision()])
    for board in range(2):
        for antenna in range(8):
            for lane in range(2):
                packet = _packet(lane, antenna)
                packet.config_generation = 5 + board * 17
                cluster.add_message(board, _message(antenna, packet))
    assert cluster.is_complete
    assert cluster.coverage_consistent
    np.testing.assert_array_equal(cluster.config_generation[0], 5)
    np.testing.assert_array_equal(cluster.config_generation[1], 22)


def test_accumulation_replaces_old_generation_without_mixing_sections():
    cluster = IQAccumCluster(_packet(0), [_Revision(), _Revision()])
    for board in range(2):
        for lane in range(2):
            cluster.add_message(board, _message(0, _packet(lane)))
    packet = _packet(1)
    packet.config_generation += 1
    assert cluster.add_message(0, _message(0, packet))
    assert not cluster.completion[0, 0, 0]
    assert cluster.completion[1, 0, 0]
    assert np.isnan(cluster.iq_sections[0, 0, 0, 0]).all()
    assert not cluster.add_message(0, _message(0, _packet(0)))
    packet = _packet(0)
    packet.config_generation += 1
    assert cluster.add_message(0, _message(0, packet))
    assert cluster.completion[0, 0, 0]
    assert cluster.config_generation[0, 0, 0] == 6


def test_accumulation_generation_wrap():
    cluster = IQAccumCluster(_packet(0), [_Revision()])
    packet = _packet(0)
    packet.config_generation = 0xFFFFFFFF
    cluster.add_message(0, _message(0, packet))
    newer = _packet(0)
    newer.config_generation = 1
    assert cluster.add_message(0, _message(0, newer))
    assert not cluster.add_message(0, _message(1, packet))
    assert cluster.config_generation[0, 0, 0] == 1


def test_accumulation_key_is_shared_across_controller_generations():
    receiver = IQReceiver(SimpleNamespace(boards=[]))
    from espargos.iq_packet import IQAccumPacket

    first, second = IQAccumPacket.__new__(IQAccumPacket), IQAccumPacket.__new__(IQAccumPacket)
    first.__dict__.update(vars(_packet(0)))
    second.__dict__.update(vars(_packet(0)))
    second.config_generation += 10
    assert receiver._get_cluster_key(0, _message(0, first)) == receiver._get_cluster_key(1, _message(0, second))
    second.source_chunk_start += 64
    assert receiver._get_cluster_key(0, _message(0, first)) != receiver._get_cluster_key(1, _message(0, second))


def test_mixed_raw_cluster_is_released_after_all_receivers_arrive():
    receiver = IQReceiver(SimpleNamespace(boards=[]))
    receiver._expected = np.zeros((1, 2, 4), dtype=bool)
    receiver._expected[0, 0, :2] = True
    seen = []
    receiver.add_callback(seen.append)
    cluster = IQCluster(1, [_Revision()], gain_phase_compensation=False)
    cluster._completion[0, 0, 0] = True
    assert not receiver._on_cluster_updated("chunks", 1, cluster)
    assert not seen
    cluster._completion[0, 0, 1] = True
    assert not cluster.is_complete
    assert receiver._on_cluster_updated("chunks", 1, cluster)
    assert seen == [cluster]


def test_accumulated_peak_precedes_phase_correction_and_resets_with_generation():
    packet = _packet(0)
    packet.decode_iq = lambda: np.full(256, 400 + 400j, dtype=np.complex64)
    cluster = IQAccumCluster(packet, [_Revision()], gain_phase_compensation=True)
    cluster.add_message(0, _message(0, packet))
    assert cluster.adc_peak[0, 0, 0, 0] == 400
    # Discard old peak readings when the board starts a new generation.
    newer = _packet(1)
    newer.config_generation += 1
    cluster.add_message(0, _message(0, newer))
    assert np.isnan(cluster.adc_peak[0, 0, 0, 0])
    assert cluster.adc_peak[0, 0, 0, 1] == 1
