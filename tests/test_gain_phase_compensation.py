import unittest
from types import SimpleNamespace

import numpy as np

import espargos
from espargos.iq_backlog import IQBacklog
from espargos.iq_cluster import CHUNK_SAMPLES, IQCluster
from espargos.iq_receiver import IQReceiver


class _FakePool:
    shape = (1, 2, 4)

    def __init__(self):
        self.callback = None

    def add_callback(self, callback, **kwargs):
        self.callback = callback
        return callback

    def remove_callback(self, callback):
        self.callback = None
        return True


class _Revision:
    def antenna_id_to_row_col(self, antenna_id):
        return divmod(antenna_id, 4)


class IQGainPhaseTests(unittest.TestCase):
    def test_pool_defaults_enabled_and_can_be_toggled(self):
        pool = IQReceiver(SimpleNamespace(boards=[]))
        self.assertTrue(pool.gain_phase_compensation)
        pool.gain_phase_compensation = False
        self.assertFalse(pool.gain_phase_compensation)

    def test_cluster_uses_per_sample_gain_metadata(self):
        phase = np.asarray(espargos.gain_phase_calibration.GAIN_PHASE_SHIFT_BY_GAIN_RAD)
        gain_pattern = np.resize(np.asarray([31, 32, 40, 41], dtype=np.uint8), CHUNK_SAMPLES)
        received = np.exp(1j * phase[gain_pattern]).astype(np.complex64)
        payload = SimpleNamespace(
            chunk_counter=1,
            decode_iq=lambda: received,
            sample_rx_gain=lambda: gain_pattern,
        )
        cluster = IQCluster(1, [_Revision()], gain_phase_compensation=True)
        cluster.add_message(0, SimpleNamespace(antenna_id=0, payload=payload))

        np.testing.assert_allclose(cluster.iq[0, 0, 0], 1.0, atol=2e-6)
        np.testing.assert_array_equal(cluster.sample_rx_gain[0, 0, 0], gain_pattern)

        raw_cluster = IQCluster(2, [_Revision()], gain_phase_compensation=False)
        raw_cluster.add_message(0, SimpleNamespace(antenna_id=0, payload=payload))
        np.testing.assert_array_equal(raw_cluster.iq[0, 0, 0], received)

    def test_cluster_keeps_finite_iq_when_all_gain_metadata_is_invalid(self):
        received = np.asarray([complex(index, -index) for index in range(CHUNK_SAMPLES)], dtype=np.complex64)
        invalid_gains = np.full(CHUNK_SAMPLES, 0x7F, dtype=np.uint8)
        payload = SimpleNamespace(
            chunk_counter=1,
            decode_iq=lambda: received,
            sample_rx_gain=lambda: invalid_gains,
        )

        cluster = IQCluster(3, [_Revision()], gain_phase_compensation=True)
        cluster.add_message(0, SimpleNamespace(antenna_id=0, payload=payload))

        np.testing.assert_array_equal(cluster.iq[0, 0, 0], received)
        np.testing.assert_array_equal(cluster.sample_rx_gain[0, 0, 0], invalid_gains)

    def test_backlog_stores_cluster_data_without_another_correction(self):
        pool = _FakePool()
        backlog = IQBacklog(pool, fields={"iq"}, size=2)
        iq = np.ones((1, 2, 4, CHUNK_SAMPLES), dtype=np.complex64)
        pool.callback(SimpleNamespace(iq=iq, chunk_index=1, host_timestamp=0.0))
        np.testing.assert_array_equal(backlog.get("iq")[-1], iq)


if __name__ == "__main__":
    unittest.main()


def test_integer_gain_fast_path_matches_float_metadata_and_broadcasting():
    from espargos.gain_phase_calibration import apply
    for gains in [np.arange(77, dtype=np.uint8), np.array(40), np.array([0, 31, 76])]:
        values = np.ones(gains.shape + (5,), dtype=np.complex64)
        np.testing.assert_array_equal(apply(values, gains), apply(values, gains.astype(float)))


def test_invalid_metadata_behavior_is_preserved():
    import pytest
    from espargos.gain_phase_calibration import apply
    for gains in [np.array([-1, 40]), np.array([127, 40], dtype=np.uint8), np.array([1.5, 40])]:
        with pytest.raises(ValueError):
            apply(np.ones(2, dtype=np.complex64), gains)
        assert np.isnan(apply(np.array([np.nan, 1], dtype=np.complex64), gains)[0])
