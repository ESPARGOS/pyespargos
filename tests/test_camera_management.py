"""Races between automatic capture management and interactive settings."""

import threading
from types import SimpleNamespace
import pytest

pytest.importorskip("PyQt6")
from demos.iq.camera.main import EspargosIQCamera


def test_threshold_update_cannot_restore_a_stale_trigger_mode():
    applied = []
    old = {"mode": "signal", "threshold": 64}
    current = {"mode": "interval", "interval_chunks": 1024, "burst_chunks": 1}
    app = SimpleNamespace(
        autoThreshold=True,
        _noise=None,
        _capture_time=0,
        iqcontrol=SimpleNamespace(referenceBusy=False, _reference_io_lock=threading.Lock(), apply_capture_patch=applied.append),
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
        autoThreshold=True,
        _noise=None,
        _capture_time=0,
        iqcontrol=SimpleNamespace(referenceBusy=False, _reference_io_lock=threading.Lock(), apply_capture_patch=applied.append),
        receiver=SimpleNamespace(get_config=lambda: {"trigger": {"mode": "signal", "threshold": 128}}, apply_config=applied.append),
    )
    EspargosIQCamera._adjust_trigger_threshold(app, {"trigger": {"mode": "signal", "threshold": 64}})
    assert applied == []


def test_late_repaired_capture_cannot_rewind_live_camera():
    import queue
    import numpy as np
    from espargos import IQSignalCapture

    app = SimpleNamespace(
        _stop=threading.Event(),
        receiver=SimpleNamespace(calibrating=False, calibration_applies_to=lambda _: True),
        _latest_signal_position=None,
        dropped_captures=0,
        queue_dropped_captures=0,
        late_captures=0,
        _pending=queue.Queue(),
    )

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


@pytest.mark.parametrize("boards, mode, expected", [(1, "auto", "signal"), (4, "auto", "interval"), (1, "interval", "interval"), (4, "accumulate", "accumulate")])
def test_camera_chooses_supported_trigger_and_sends_only_its_fields(monkeypatch, boards, mode, expected):
    from demos.iq.common.application import ESPARGOSIQApplication

    patches = []
    owner = SimpleNamespace(boards=[None] * boards)
    capture = dict(EspargosIQCamera.DEFAULT_CONFIG["capture"])
    capture["trigger"] = {**capture["trigger"], "mode": mode}
    config = SimpleNamespace(get=lambda _: capture)
    # Real QObject type for the base-class call, without opening a GUI.
    app = EspargosIQCamera.__new__(EspargosIQCamera)
    app.appconfig = config
    receiver = SimpleNamespace(apply_config=patches.append)
    monkeypatch.setattr(ESPARGOSIQApplication, "_create_receiver", lambda self, pool: receiver)
    assert app._create_receiver(owner) is receiver
    trigger = patches[0]["trigger"]
    assert trigger["mode"] == expected
    assert ("capture_chunks" in trigger) == (expected == "signal")
    assert ("interval_chunks" in trigger) == (expected != "signal")
    if expected == "interval":
        assert trigger["interval_chunks"] == 4096


@pytest.mark.parametrize("power,age,quiet,expected", [(1.0, 0, 0, 511), (None, 5, 60, 61)])
def test_auto_threshold_respects_firmware_limits(power, age, quiet, expected):
    import time
    import numpy as np

    trigger = {"mode": "signal", "threshold": 120, "quiet_threshold": quiet}
    updates = []
    app = SimpleNamespace(
        autoThreshold=True,
        _noise=SimpleNamespace(power=np.array([power])) if power is not None else None,
        _capture_time=time.monotonic() - age,
        iqcontrol=SimpleNamespace(referenceBusy=False, _reference_io_lock=threading.Lock(), apply_capture_patch=updates.append),
        receiver=SimpleNamespace(get_config=lambda: {"trigger": trigger}, apply_config=updates.append),
    )
    EspargosIQCamera._adjust_trigger_threshold(app, {"trigger": trigger})
    assert updates == [{"trigger": {**trigger, "threshold": expected}}]


@pytest.mark.parametrize("reboot", [False, True])
def test_reconnect_restores_requested_settings_only_after_role_loss(reboot):
    from unittest.mock import Mock

    camera = EspargosIQCamera.__new__(EspargosIQCamera)
    camera.pool = Mock()
    healthy, disconnected = Mock(), Mock()
    healthy.iq.get_config.return_value = {"modes": ["iq_rx"] * 8}
    disconnected.iq.get_config.return_value = {"modes": ["wifi" if reboot else "iq_rx"] * 8}
    camera.pool.boards = [healthy, disconnected]
    camera.receiver = Mock()
    from demos.iq.common.application import IQController

    camera.pool.iq = camera.receiver
    camera.iqcontrol = IQController(camera.pool, capture_config={"rf_freq_hz": 2457000000, "trigger": {"mode": "interval"}})
    camera.iqcontrol._check_calibration_config = Mock()
    camera.iqcontrol.apply_config_json('{"rx_gain": 28}')
    camera.receiver.apply_config.reset_mock()
    camera.iqcontrol._set_sync_needed(False)
    camera._latest_signal_position = (3, 45, 56)
    camera._reconnect_capture()
    camera.pool.reconnect.assert_called_once()
    camera.receiver.clear_chunks.assert_called_once()
    assert camera._latest_signal_position is None
    if reboot:
        camera.pool.configure.assert_called_once_with(mode="iq_rx")
        camera.receiver.apply_config.assert_called_once_with({"rf_freq_hz": 2457000000, "rx_gain": 28, "trigger": {"mode": "interval"}})
        assert camera.iqcontrol.syncNeeded
    else:
        camera.pool.configure.assert_not_called()
        camera.receiver.apply_config.assert_not_called()
        assert not camera.iqcontrol.syncNeeded


def test_feed_change_discards_old_image_and_gain_edit_preserves_it():
    from unittest.mock import Mock

    camera = SimpleNamespace(_display_feed=2, clearImage=Mock(), iqcontrol=SimpleNamespace(syncNeeded=False))
    for patch in ({"rx_gain": 31}, {"rf_switch": 2}, {"rf_switch": 3}):
        EspargosIQCamera._capture_config_applied(camera, patch)
    camera.clearImage.assert_called_once()
    assert camera._display_feed == 3


def test_rejected_startup_settings_release_hardware_before_pool_closes(monkeypatch):
    from unittest.mock import Mock
    from demos.iq.common.application import ESPARGOSIQApplication

    owner = Mock(boards=[object()])
    receiver = Mock()
    receiver.apply_config.side_effect = ValueError("holdoff outside range")
    app = EspargosIQCamera.__new__(EspargosIQCamera)
    app.appconfig = SimpleNamespace(get=lambda _: EspargosIQCamera.DEFAULT_CONFIG["capture"])
    monkeypatch.setattr(ESPARGOSIQApplication, "_create_receiver", lambda self, pool: receiver)
    with pytest.raises(ValueError, match="holdoff"):
        app._create_receiver(owner)
    owner.configure.assert_called_once_with(mode="wifi")


def test_queued_initialization_after_shutdown_cannot_reopen_the_pool():
    stopped = threading.Event()
    stopped.set()
    EspargosIQCamera._start_iq_camera(SimpleNamespace(_stop=stopped))


def test_signal_previews_use_one_grid_and_never_rewind_a_complete_capture():
    import queue
    from espargos import IQSignalCapture
    from test_iq_signal_capture import _cluster

    camera = EspargosIQCamera.__new__(EspargosIQCamera)
    camera._stop = threading.Event()
    camera._pending = queue.Queue(maxsize=1)
    camera._latest_signal_position = None
    camera.dropped_captures = camera.queue_dropped_captures = camera.late_captures = 0
    camera.receiver = SimpleNamespace(calibrating=False, calibration_applies_to=lambda _: True)

    def part(event, offset):
        cluster = _cluster(event, offset, chunk_count=56)
        cluster.signal_board_index = 0
        return cluster

    camera._on_iq_cluster(part(10, 0))  # Between preview offsets.
    mismatched = part(10, 4)
    mismatched._source_chunk_index.flat[0] += 1
    camera._on_iq_cluster(mismatched)
    assert camera._pending.empty()
    unsynchronized = part(10, 4)
    unsynchronized._sync_info[:] = 0
    camera._on_iq_cluster(unsynchronized)
    assert camera._pending.empty()

    first = part(10, 4)
    camera._on_iq_cluster(first)
    camera._on_iq_cluster(part(10, 12))
    assert camera._pending.get_nowait()[1] is first
    complete = IQSignalCapture(10, 80_000_000, 2457000000, first.config_generation, first.fire_time_ns, 0, first.source_chunk_index, first.source_chunk_index + 55, first.iq.repeat(56, axis=-1), first.sample_rx_gain.repeat(56, axis=-1))
    camera._on_capture(complete)
    camera._on_iq_cluster(part(11, 4))
    assert camera._pending.get_nowait()[1] is complete
    camera._on_iq_cluster(part(10, 20))
    assert camera._pending.empty()
    newer = part(11, 12)
    camera._on_iq_cluster(newer)
    assert camera._pending.get_nowait()[1] is newer
    camera._on_capture(complete)
    assert camera._pending.empty()


@pytest.mark.parametrize(
    "mode,period,role,expected",
    [
        ("signal", 4096, "iq_rx", None),
        ("interval", 3000000, "iq_rx", None),
        ("interval", 4096, "iq_rx", "Waiting for synchronized interval captures"),
        ("interval", 4096, "wifi", "Receiver left IQ mode"),
    ],
)
def test_silent_scheduled_capture_and_role_loss(mode, period, role, expected):
    import time
    from unittest.mock import Mock

    config = {"modes": ["iq_rx"] * 8, "trigger": {"mode": mode, "interval_chunks": period}, "adc_decimation": 1}
    second = Mock()
    second.iq.get_config.return_value = {"modes": [role] * 8}
    state = SimpleNamespace(pool=SimpleNamespace(boards=[Mock(), second]), _capture_time=time.monotonic() - 10, iqcontrol=Mock(syncNeeded=False))
    status = EspargosIQCamera._receiver_status(state, config)
    if expected:
        assert status.startswith(expected)
    else:
        assert status is None
        second.iq.get_config.assert_not_called()
    if role != "iq_rx":
        state.iqcontrol._set_sync_needed.assert_called_once_with(True)
    else:
        state.iqcontrol._set_sync_needed.assert_not_called()
