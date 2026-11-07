from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from espargos._iq_calibration import reference


@pytest.mark.parametrize("failure", [False, True])
def test_onset_opens_narrow_filters_and_restores_them(failure):
    calls = []
    boards = []
    for index, bandwidth in enumerate((20, 0, 40)):
        boards.append(
            SimpleNamespace(
                iq=SimpleNamespace(
                    get_config=lambda bw=bandwidth: {"filter_bw_mhz": bw},
                    set_config=lambda cfg, i=index: calls.append((i, cfg)),
                )
            )
        )
    pool = SimpleNamespace(boards=boards)
    scope = SimpleNamespace(active=[0, 1, 2])

    def onset(*args):
        assert calls == [(0, {"filter_bw_mhz": 40})]
        if failure:
            raise ValueError("missing reference")
        return "synchronized"

    with patch.object(reference, "_acquire_grid", side_effect=onset):
        if failure:
            with pytest.raises(ValueError, match="missing reference"):
                reference.acquire_grid(pool, 2472e6, scope=scope)
        else:
            assert reference.acquire_grid(pool, 2472e6, scope=scope) == "synchronized"
    assert calls == [(0, {"filter_bw_mhz": 40}), (0, {"filter_bw_mhz": 20})]


def test_filter_restoration_failure_cannot_publish_sync_success():
    iq = SimpleNamespace(get_config=lambda: {"filter_bw_mhz": 20}, set_config=Mock(side_effect=[None, OSError("offline")]))
    with patch.object(reference, "_acquire_grid", return_value="synchronized"):
        with pytest.raises(RuntimeError, match="restore analog filters"):
            reference.acquire_grid(SimpleNamespace(boards=[SimpleNamespace(iq=iq)]), 2472e6, scope=SimpleNamespace(active=[0]))
