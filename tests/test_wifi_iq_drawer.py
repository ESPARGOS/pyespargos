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
        assert [c.args[0] for c in pool.wifi_iq.configure.call_args_list] == [
            {"rx_gain": 24}, {"max_pps": 50, "rx_gain": 36}
        ]
        pool.wifi_iq.calibrate.assert_not_called()
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
