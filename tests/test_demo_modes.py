from unittest.mock import Mock

import pytest

from demos.ble.common.application import ESPARGOSBluetoothApplication
from demos.iq.common.application import ESPARGOSIQApplication, IQCaptureApplication
from demos.wifi.common.application import ESPARGOSWiFiApplication
from demos.wifi_iq.common.application import ESPARGOSWiFiIQApplication


@pytest.mark.parametrize(
    ("app", "profile", "role"),
    [
        (ESPARGOSWiFiApplication, "wifi", "wifi"),
        (ESPARGOSWiFiIQApplication, "wifi_iq", "wifi_iq"),
        (ESPARGOSBluetoothApplication, "ble", "ble"),
        (IQCaptureApplication, "iq", "iq_rx"),
        (ESPARGOSIQApplication, "iq", None),
    ],
)
def test_startup_sets_measurement_board_modes(app, profile, role):
    boards = [Mock(host=host, api_version=(5, 0)) for host in ("front", "rear")]
    pool = Mock(boards=boards)
    app.request_mode(pool)
    for board in boards:
        board.control.post_json.assert_called_once_with("set_controller_mode", {"mode": profile})
    if role is None:
        pool.configure.assert_not_called()
    else:
        pool.configure.assert_called_once_with(mode=role)
    pool.reference_controller.control.post_json.assert_not_called()


def test_wifi_startup_with_mixed_firmware_generations():
    legacy, sensor_only, current = [Mock(host=str(i), api_version=v) for i, v in enumerate([(3, 0), (4, 0), (5, 0)])]
    pool = Mock(boards=[legacy, sensor_only, current])
    ESPARGOSWiFiApplication.request_mode(pool)
    legacy.control.post_json.assert_not_called()
    sensor_only.control.post_json.assert_not_called()
    current.control.post_json.assert_called_once_with("set_controller_mode", {"mode": "wifi"})
    pool.select.assert_called_once_with(boards=[sensor_only.host, current.host])
    pool.select.return_value.configure.assert_called_once_with(mode="wifi")
