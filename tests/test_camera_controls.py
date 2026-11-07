"""Interactive IQ settings stay responsive during slow board RPCs."""

import threading
from types import SimpleNamespace
import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QCoreApplication
from demos.iq.common.application import IQController


def test_receiver_edits_coalesce_off_gui_and_replace_trigger_mode():
    app = QCoreApplication.instance() or QCoreApplication([])
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    radio, captures, threads = [], [], []

    def configure(**config):
        threads.append(threading.get_ident())
        radio.append(config)
        if len(radio) == 1:
            entered.set()
            assert release.wait(2)

    def apply(config):
        captures.append(config)
        finished.set()

    receiver = SimpleNamespace(add_callback=lambda cb: None, add_accumulation_callback=lambda cb: None, calibration=object(), calibration_center_frequency=2472000000, calibration_sample_rate=80000000, apply_config=apply)
    controller = IQController(SimpleNamespace(iq=receiver, configure=configure))
    try:
        controller.queue_config_json('{"rx_gain":35}')
        assert entered.wait(1)  # GUI call already returned while RPC is blocked
        controller.queue_config_json('{"rx_gain":36,"trigger":{"mode":"signal","threshold":64}}')
        controller.queue_config_json('{"rx_gain":37,"trigger":{"mode":"interval","interval_chunks":1024,"burst_chunks":1}}')
        release.set()
        assert finished.wait(1)
    finally:
        release.set()
        controller.close()
    assert radio == [{"rx_gain": 35}, {"rx_gain": 37}]
    assert captures == [{"trigger": {"mode": "interval", "interval_chunks": 1024, "burst_chunks": 1}}]
    assert all(t != threading.get_ident() for t in threads)
    controller.queue_config_json('{"rx_gain":40}')
    assert len(radio) == 2


def test_closed_controller_does_not_start_hardware_edits():
    app = QCoreApplication.instance() or QCoreApplication([])
    calls = []
    receiver = SimpleNamespace(add_callback=lambda cb: None, add_accumulation_callback=lambda cb: None)
    controller = IQController(SimpleNamespace(iq=receiver, configure=lambda **cfg: calls.append(cfg)))
    controller.close()
    controller.apply_config_json('{"rx_gain":40}')
    controller.queue_config_json('{"rx_gain":40}')
    assert calls == []


def test_shutdown_waits_for_calibration_before_pool_can_close():
    app = QCoreApplication.instance() or QCoreApplication([])
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def calibrate():
        entered.set()
        assert release.wait(2)

    receiver = SimpleNamespace(add_callback=lambda cb: None, add_accumulation_callback=lambda cb: None)
    controller = IQController(SimpleNamespace(iq=receiver, calibrate=calibrate))
    controller.sync_async()
    assert entered.wait(1)

    def close():
        controller.close()
        closed.set()

    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert not closed.wait(0.02)
    finally:
        release.set()
        closer.join(1)
    assert closed.is_set()
    assert not controller.referenceBusy
    assert not controller._begin_reference()
