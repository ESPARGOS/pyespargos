from types import SimpleNamespace

import numpy as np
import pytest

from espargos.iq_cluster import IQCluster
from espargos.iq_reference import collect


class Revision:
    @staticmethod
    def antenna_id_to_row_col(antid):
        return divmod(antid, 4)


def cluster(value):
    c = IQCluster(0, [Revision()], gain_phase_compensation=True)
    for antid in range(8):
        packet = SimpleNamespace(
            decode_iq=lambda: np.tile(np.array([value, -value], np.complex64), 128),
            sample_rx_gain=lambda: np.full(256, 42, np.uint8),
            chunk_counter=0,
            config_generation=1,
            sync_info=(1 << 16) | 1,
        )
        c.add_message(0, SimpleNamespace(antenna_id=antid, payload=packet))
    return c


def pool(c):
    return SimpleNamespace(
        boards=[SimpleNamespace(iq=SimpleNamespace(get_config=lambda: {"config_generation": 1}))],
        add_callback=lambda cb, **kwargs: cb(c),
        remove_callback=lambda _: None,
        processing=True,
    )


def test_rotation_does_not_create_adc_clipping():
    c = cluster(400 + 400j)
    assert np.max(abs(c.iq.imag)) > 510
    np.testing.assert_array_equal(c.adc_peak, 400)
    assert collect(pool(c), count=1, timeout=0.01, seqs=[1]) == [c]


def test_rotation_cannot_hide_adc_clipping():
    c = cluster(511 + 0j)
    assert np.max(abs(c.iq.real)) < 510
    assert np.max(abs(c.iq.imag)) < 510
    np.testing.assert_array_equal(c.adc_peak, 511)
    with pytest.raises(RuntimeError, match="Only 0/1"):
        collect(pool(c), count=1, timeout=0.01, seqs=[1])
