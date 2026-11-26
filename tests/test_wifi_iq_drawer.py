"""Radio changes and transmitter selection in the WiFi IQ camera."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication
from demos.wifi.common.csi_backlog_settings import CSIBacklogSettings
from demos.wifi_iq.common.receiver import WiFiIQControls
from espargos import CSIBacklog, WiFiIQReceiver, WiFiTransceiver


def test_failed_calibration_keeps_actual_settings_and_allows_retry():
    app = QApplication.instance() or QApplication([])
    pool = Mock()
    pool.boards = [Mock()]
    radio = pool.boards[0].wifi_iq
    radio.get_config.return_value = {"channel": 5, "rx_gain": 30}
    pool.wifi_iq.calibrate.side_effect = [TimeoutError("No reference packets"), None]
    controls = WiFiIQControls(pool)
    try:
        controls.start(5, 30).result(timeout=2)
        app.processEvents()
        assert not controls.busy
        assert controls.settings["channel"] == 5
        assert controls.status == "No reference packets"

        controls.calibrate()
        controls.close()
        assert pool.wifi_iq.calibrate.call_count == 2
    finally:
        controls.close()


def test_edits_during_radio_update_apply_latest_values_without_recalibrating():
    import time

    app = QApplication.instance() or QApplication([])
    pool = Mock()
    pool.boards = [Mock()]
    pool.wifi_iq.has_calibration.return_value = True
    pool.wifi_iq.configure.side_effect = lambda settings: settings
    controls = WiFiIQControls(pool)
    try:
        controls.apply({"rx_gain": 24})
        controls.apply({"max_pps": 50, "rx_gain": 30})
        controls.apply({"rx_gain": 36})
        deadline = time.monotonic() + 2
        while controls.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert not controls.busy
        assert [c.args[0] for c in pool.wifi_iq.configure.call_args_list] == [{"rx_gain": 24}, {"max_pps": 50, "rx_gain": 36}]
        pool.wifi_iq.calibrate.assert_not_called()
    finally:
        controls.close()


def test_channel_change_waits_for_manual_calibration_and_tracks_external_loss():
    app = QApplication.instance() or QApplication([])
    pool = Mock()
    pool.boards = [Mock()]
    receiver = pool.wifi_iq
    receiver.has_calibration.return_value = True

    def configure(settings):
        receiver.has_calibration.return_value = False
        return settings

    receiver.configure.side_effect = configure
    receiver.calibrate.side_effect = lambda: setattr(receiver.has_calibration, "return_value", True)
    controls = WiFiIQControls(pool)
    try:
        controls._refresh_calibration()
        assert controls.calibrated
        controls._submit({"channel": 7}).result(timeout=2)
        app.processEvents()
        assert not controls.calibrated
        receiver.calibrate.assert_not_called()
        controls.calibrate()
        controls._executor.submit(lambda: None).result(timeout=2)
        app.processEvents()
        assert controls.calibrated
        receiver.has_calibration.return_value = False
        controls._refresh_calibration()
        assert not controls.calibrated
        receiver.calibrate.assert_called_once()
    finally:
        controls.close()


def test_mac_filter_uses_backlog_without_touching_radio():
    pool = Mock()
    pool.boards = [Mock()]
    controls = WiFiIQControls(pool)
    backlog = Mock()
    controls.attach_backlog(backlog)
    try:
        controls.setMacFilter("3C:DC:75:88:88:01")
        selected = backlog.add_filter.call_args.args[0]
        assert selected.matches(Mock(source_mac="3cdc75888801"))
        controls.setMacFilter("invalid")
        assert controls.macFilter == "3c:dc:75:88:88:01"
        controls.setMacFilter("")
        backlog.remove_filter.assert_called_once_with(selected)
        pool.boards[0].wifi_iq.set_config.assert_not_called()
    finally:
        controls.close()


@pytest.mark.parametrize("receiver_type", [WiFiIQReceiver, WiFiTransceiver])
def test_backlog_drawer_applies_only_fields_provided_by_receiver(receiver_type):
    app = QApplication.instance() or QApplication([])
    receiver = receiver_type(SimpleNamespace(boards=[]))
    backlog = CSIBacklog(receiver, fields={"lltf", "mac"})
    formats = dict.fromkeys(("he20", "ht20", "ht40"), True)
    settings = CSIBacklogSettings({"size": 20, "fields": formats, "filters": {}})
    try:
        settings.set_backlog(backlog)
        settings.cfgman.updateAppState.emit({"fields": formats, "size": 12})
        app.processEvents()
        expected = set(formats) if receiver_type is WiFiTransceiver else set()
        assert backlog.fields == {"lltf", "mac"} | expected
        assert backlog.size == 12
        assert settings.cfgman.get("fields") == {f: f in expected for f in formats}
    finally:
        backlog.close()
        receiver._close()


def test_camera_expires_last_packet_and_clears_power_display_only_once():
    import numpy as np
    from demos.wifi.camera.backend import CSICamera

    now = 500
    app = SimpleNamespace(
        get_backlog_csi=Mock(return_value=("lltf", np.ones((1, 1, 2, 4, 53)), np.array([now - 2.0]), np.zeros((1, 6)), np.full((1, 1, 2, 4), 2))),
        _rendered_packets=("lltf", b"previous"),
        overlay=Mock(),
        power_unit="dBFS",
        level_fields=(),
        steering_vectors_2d=np.zeros((2, 4, 16, 8)),
        appconfig=Mock(),
    )
    app.appconfig.get.return_value = 0.5
    app._clear_measurements = lambda: CSICamera._clear_measurements(app)
    from unittest.mock import patch

    with patch("demos.wifi.camera.backend.time.time", return_value=now):
        CSICamera.updateSpatialSpectrum(app)
        CSICamera.updateSpatialSpectrum(app)
    app.overlay.publish_receiver_statistics.assert_called_once_with(-np.inf, "dBFS", 0)
    name, rgba = app.overlay.publish_image.call_args.args
    assert name == "spatial" and rgba.shape == (8, 16, 4) and not rgba.any()
    app.overlay.publish_image.assert_called_once()


def test_bartlett_renders_known_wifi_iq_polarization_with_missing_feed():
    import numpy as np
    from espargos.array_processing import steering_vectors_2d
    from espargos.csi_processing import get_frequencies_lltf
    from espargos.polarization import antenna_response
    from demos.wifi_iq.camera.main import WiFiIQCamera
    from demos.common.config_manager import ConfigManager

    class Orientation:
        def rotation_matrix(self):
            return np.eye(2)

    qt_app = QApplication.instance() or QApplication([])
    cfg = ConfigManager(WiFiIQCamera.DEFAULT_CONFIG)
    cfg.set({"beamformer": {"type": "Bartlett", "polarization_mode": "show"}})
    frequencies = get_frequencies_lltf(6)
    grid = steering_vectors_2d(2, 4, 32, 24, frequency_hz=frequencies[26], pixel_centers=True)
    target = (18, 9)
    incident = np.array([1, 0.5j])
    feeds = grid[:, :, target[0], target[1], None, None] * (antenna_response(frequencies) @ incident)
    patterns = np.indices((2, 4)).sum(axis=0) % 2
    routes = np.stack([patterns, 1 - patterns, np.zeros_like(patterns), np.ones_like(patterns)])
    routes[:, 1, 3] = 0
    values = np.take_along_axis(feeds[None, ...], routes[..., None, None], axis=-1)[..., 0]
    gains = np.where(routes == 0, 12, 18)
    values *= 10.0 ** (gains[..., None] / 20)
    app = SimpleNamespace(
        appconfig=cfg,
        controls=SimpleNamespace(settings={"channel": 6, "gain_mode": "auto"}),
        antenna_orientations=np.full((2, 4), Orientation(), object),
        additional_calibration=None,
        indexing_matrix=np.arange(8).reshape(2, 4),
        steering_vectors_2d=grid,
        n_rows=2,
        n_cols=4,
        _rendered_packets=None,
        overlay=Mock(),
        level_fields=("rx_gain",),
        power_unit="dBFS",
        get_backlog_csi=lambda *_args, **_kwargs: ("lltf", values[:, None], np.arange(4.0), np.zeros((4, 6)), routes[:, None] + 2, gains[:, None]),
        _polarization_channels=lambda: (6, 0),
        received_power=lambda csi, levels: np.zeros(csi.shape[:-1]),
    )
    app.normalize_channel = lambda c, levels: WiFiIQCamera.normalize_channel(app, c, levels)
    app.correct_polarization = lambda c, fmt: WiFiIQCamera.correct_polarization(app, c, fmt)
    WiFiIQCamera.updateSpatialSpectrum(app)
    assert np.unravel_index(app.beamspace_power.argmax(), grid.shape[-2:]) == target
    vertical, horizontal = app.overlay.publish_polarization.call_args.args
    np.testing.assert_allclose(vertical[target] / horizontal[target], incident[1], atol=0.03)
