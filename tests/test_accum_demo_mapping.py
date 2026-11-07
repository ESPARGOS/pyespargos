import importlib.util
import pathlib
import sys
import threading
from types import SimpleNamespace

import numpy as np

from espargos.iq_accum_cluster import IQAccumCluster

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Revision:
    @staticmethod
    def antenna_id_to_row_col(antenna_id):
        return divmod(antenna_id, 4)


def _load_demo(name, relative_path):
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _packet(lane, antenna_id):
    value = 100 * antenna_id + lane
    return SimpleNamespace(
        fire_time_ns=1000000,
        window_index=9,
        source_chunk_start=8192,
        source_chunk_end=16384,
        vector_chunks=4,
        vector_chunk_index=lane,
        nominal_chunks=2048,
        accumulated_chunks=50 + antenna_id,
        coverage_hash=1000 + lane + antenna_id,
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


def _cluster():
    cluster = IQAccumCluster(_packet(0, 0), [_Revision()])
    for antenna_id in range(8):
        for lane in range(4):
            packet = _packet(lane, antenna_id)
            cluster.add_message(0, SimpleNamespace(antenna_id=antenna_id, payload=packet))
    assert cluster.is_complete
    return cluster


def test_signal_analyzer_splits_accumulation_into_display_sections():
    module = _load_demo("iq_signal_analyzer_test", "demos/iq-signal-analyzer/iq-signal-analyzer.py")
    fake = SimpleNamespace(
        _pending_lock=threading.Lock(),
        _pending_clusters=[(0.0, _cluster())],
        _display_epoch=None,
        _display_epoch_time=0.0,
        active_board=0,
        receiver=SimpleNamespace(boards=[SimpleNamespace(revision=_Revision())]),
    )

    sets = module.EspargosSignalAnalyzer._drain_sets(fake)

    assert [index for index, _per in sets] == [8192, 8193, 8194, 8195]
    for lane, (_index, per) in enumerate(sets):
        assert set(per) == set(range(8))
        for antenna_id in range(8):
            np.testing.assert_array_equal(per[antenna_id], 100 * antenna_id + lane + 1j * (100 * antenna_id + lane))
