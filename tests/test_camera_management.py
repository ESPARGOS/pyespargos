"""Races between automatic capture management and interactive settings."""

import threading
from types import SimpleNamespace
import pytest

pytest.importorskip("PyQt6")
from demos.camera.main import EspargosIQCamera


def test_threshold_update_cannot_restore_a_stale_trigger_mode():
    applied = []
    old = {"mode": "signal", "threshold": 64}
    current = {"mode": "interval", "interval_chunks": 1024, "burst_chunks": 1}
    app = SimpleNamespace(
        _noise=None,
        _capture_time=0,
        iqcontrol=SimpleNamespace(referenceBusy=False, _reference_io_lock=threading.Lock()),
        receiver=SimpleNamespace(get_config=lambda: {"trigger": current}, apply_config=applied.append),
    )
    EspargosIQCamera._adjust_trigger_threshold(app, {"trigger": old})
    assert applied == []
    current = old.copy()
    EspargosIQCamera._adjust_trigger_threshold(app, {"trigger": old})
    assert applied == [{"trigger": {"mode": "signal", "threshold": 32}}]
    assert old["threshold"] == 64


def test_threshold_update_preserves_a_new_manual_threshold():
    applied = []
    app = SimpleNamespace(
        _noise=None,
        _capture_time=0,
        iqcontrol=SimpleNamespace(referenceBusy=False, _reference_io_lock=threading.Lock()),
        receiver=SimpleNamespace(get_config=lambda: {"trigger": {"mode": "signal", "threshold": 128}}, apply_config=applied.append),
    )
    EspargosIQCamera._adjust_trigger_threshold(app, {"trigger": {"mode": "signal", "threshold": 64}})
    assert applied == []


def test_late_repaired_capture_cannot_rewind_live_camera():
    import queue
    import numpy as np
    from espargos import IQSignalCapture

    app = SimpleNamespace(_capture_arrived=threading.Event(), _stop=threading.Event(), receiver=SimpleNamespace(calibrating=False, calibration_applies_to=lambda _: True), _latest_signal_identity=None, dropped_captures=0, queue_dropped_captures=0, late_captures=0, _pending=queue.Queue())

    def capture(generation, identity):
        data = np.zeros((1, 2, 4))
        return IQSignalCapture(identity, 80000000, 2472000000, np.full((1, 2, 4), generation), data, 0, data, data, data, data)

    for generation, identity in [(7, 10), (7, 9), (7, 10), (8, 1), (9, 0xFFFFFFFE), (9, 0)]:
        EspargosIQCamera._on_capture(app, capture(generation, identity))
    queued = []
    while not app._pending.empty():
        queued.append(app._pending.get()[1].capture_id)
    assert queued == [10, 1, 0xFFFFFFFE, 0]
    assert app.dropped_captures == 2
