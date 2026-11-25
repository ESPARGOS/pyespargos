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
    patches, threads = [], []

    def apply(config):
        threads.append(threading.get_ident())
        patches.append(config)
        if len(patches) == 1:
            entered.set()
            assert release.wait(2)

        else:
            finished.set()

    receiver = SimpleNamespace(
        add_callback=lambda cb: None,
        add_accumulation_callback=lambda cb: None,
        add_stream_callback=lambda cb: None,
        calibration=SimpleNamespace(phase_offsets=[0.0]),
        calibration_center_frequency=2472000000,
        calibration_sample_rate=80000000,
        get_config=lambda: {},
        apply_config=apply,
    )
    controller = IQController(SimpleNamespace(iq=receiver))
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
    assert patches == [{"rx_gain": 35}, {"rx_gain": 37, "trigger": {"mode": "interval", "interval_chunks": 1024, "burst_chunks": 1}}]
    assert all(t != threading.get_ident() for t in threads)
    controller.queue_config_json('{"rx_gain":40}')
    assert len(patches) == 2


def test_closed_controller_does_not_start_hardware_edits():
    app = QCoreApplication.instance() or QCoreApplication([])
    calls = []
    receiver = SimpleNamespace(add_callback=lambda cb: None, add_accumulation_callback=lambda cb: None, add_stream_callback=lambda cb: None)
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

    receiver = SimpleNamespace(add_callback=lambda cb: None, add_accumulation_callback=lambda cb: None, add_stream_callback=lambda cb: None)
    controller = IQController(SimpleNamespace(iq=receiver, boards=[], calibrate=calibrate))
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


def test_slow_receiver_snapshot_does_not_block_qt():
    import json
    import time

    app = QCoreApplication.instance() or QCoreApplication([])
    entered, release = threading.Event(), threading.Event()
    snapshot = {"adc_decimation": 1, "trigger": {"mode": "interval", "interval_chunks": 4096}}

    def get_config():
        entered.set()
        assert release.wait(2)
        return snapshot

    receiver = SimpleNamespace(get_config=get_config, add_callback=lambda _: None, add_accumulation_callback=lambda _: None, add_stream_callback=lambda _: None, calibration=None, calibration_center_frequency=None, calibration_sample_rate=None)
    controller = IQController(SimpleNamespace(iq=receiver))
    received = []
    controller.configSnapshot.connect(lambda value: received.append(json.loads(value)))
    try:
        controller.request_config()
        assert entered.wait(1)
        assert received == []
        controller.request_config()  # Opening the drawer twice shares the pending read.
        release.set()
        until = time.monotonic() + 1
        while not received and time.monotonic() < until:
            app.processEvents()
            time.sleep(0.001)
        assert received == [snapshot]
    finally:
        release.set()
        controller.close()


def test_invalidated_sensor_marks_sync_needed_without_another_capture():
    import numpy as np

    app = QCoreApplication.instance() or QCoreApplication([])
    calibration = SimpleNamespace(phase_offsets=np.zeros((1, 2, 4)))
    receiver = SimpleNamespace(add_callback=lambda _: None, add_accumulation_callback=lambda _: None, add_stream_callback=lambda _: None, calibration=calibration, calibration_center_frequency=2457000000, calibration_sample_rate=80000000)
    controller = IQController(SimpleNamespace(iq=receiver))
    controller._set_sync_needed(False)
    controller._check_calibration_config({"rx_gain": 28})
    assert not controller.syncNeeded
    calibration.phase_offsets[0, 1, 2] = np.nan
    controller._check_calibration_config({"filter_bw_mhz": 10})
    assert controller.syncNeeded
    controller.close()


def test_calibration_waits_for_preceding_receiver_edits():
    app = QCoreApplication.instance() or QCoreApplication([])
    entered, release, calibrated = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def apply(config):
        calls.append(config)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)

    def calibrate():
        calls.append("calibrate")
        calibrated.set()

    receiver = SimpleNamespace(
        add_callback=lambda _: None,
        add_stream_callback=lambda _: None,
        add_accumulation_callback=lambda _: None,
        apply_config=apply,
        calibration=None,
        calibration_center_frequency=None,
        calibration_sample_rate=None,
    )
    controller = IQController(SimpleNamespace(iq=receiver, boards=[], calibrate=calibrate))
    try:
        controller.queue_config_json('{"rx_gain": 28}')
        assert entered.wait(1)
        controller.queue_config_json('{"rf_freq_hz": 2457000000}')
        controller.sync_async()
        controller.queue_config_json('{"rx_gain": 99}')  # Controls are locked during calibration.
        release.set()
        assert calibrated.wait(1)
        assert calls == [{"rx_gain": 28}, {"rf_freq_hz": 2457000000}, "calibrate"]
    finally:
        release.set()
        controller.close()
