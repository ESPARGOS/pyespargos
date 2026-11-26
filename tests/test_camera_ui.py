"""Qt-only camera models. No device or video camera is required."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from PyQt6 import QtWidgets
    from demos.ble.common.advertisers import Advertisers
    from demos.ble.camera.main import BLECamera, BLEOverlay
    from demos.common.config_manager import ConfigManager
except ImportError:
    QtWidgets = None


@unittest.skipIf(QtWidgets is None, "Install the optional PyQt6 demo dependencies")
class CameraModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_advertiser_selection_and_bounds(self):
        model = Advertisers(selected=["00:00:00:00:00:00"])

        def record(i):
            address = f"00:00:00:00:00:{i:02x}"
            return dict(identity="public:" + address, address=address, addressType="public", name="", rssi=-45, clock=1, time=i * 3000)

        first = record(0)
        model.observe(first)
        self.assertEqual(model.rows[0]["packets"], 1)
        self.assertEqual(model.rows[0]["age"], 0.0)
        model.rows[0]["seen"] = time.monotonic() - 2.5
        model.refresh()
        self.assertAlmostEqual(model.rows[0]["age"], 2.5, delta=0.1)
        model.observe(dict(first, time=20))
        self.assertEqual(model.rows[0]["packets"], 1)
        model.observe(dict(first, time=4000, name="Test"))
        self.assertEqual(model.rows[0]["packets"], 2)
        self.assertEqual(model.rows[0]["name"], "Test")
        self.assertEqual(model.selected, {first["identity"]})
        for i in range(1, 140):
            model.observe(record(i))
        self.assertEqual(model.rowCount(), 128)
        self.assertIn(first["identity"], model.selected)
        model.selectAll(False)
        self.assertFalse(model.selected)
        model.select(first["identity"], True)
        self.assertEqual(model.selected, {first["identity"]})
        second = model.rows[-1]["identity"]
        observed = []
        model.selectionChanged.connect(lambda: observed.append(model.selected))
        model.selectOnly(second)
        self.assertEqual(observed, [{second}])
        model.selectOnly("missing")
        self.assertEqual(model.selected, {second})
        model.selectOnly(second)
        self.assertEqual(len(observed), 1)
        model.select(second, False)
        self.assertFalse(model.selected)

    def test_auto_exposure_and_age_opacity_are_separate(self):
        import numpy as np

        config = ConfigManager({"beamformer": {"resolution_azimuth": 16, "resolution_elevation": 8}, "visualization": {}, "camera": {}})
        overlay = BLEOverlay(config)
        power = np.full((16, 8), 0.8)
        overlay.publish_spatial_spectrum(power)
        image, _ = overlay._images.requestImage("spatial", None)
        initial = image.pixelColor(0, 0).green()
        overlay.publish_spatial_spectrum(power * 0.5)
        image, _ = overlay._images.requestImage("spatial", None)
        self.assertEqual(image.pixelColor(0, 0).green(), initial)
        # Apply stale-data opacity after automatic exposure, so normalization
        # cannot brighten an old measurement back up to full intensity.
        from demos.ble.common.beamforming import spectrum_intensity

        overlay.publish_spatial_intensity(spectrum_intensity(power) * 0.5)
        image, _ = overlay._images.requestImage("spatial", None)
        self.assertLess(image.pixelColor(0, 0).green(), initial / 2)
        self.assertEqual((image.width(), image.height()), (16, 8))

    def test_fresh_spectrum_is_steady_and_stale_spectrum_fades_after_contrast(self):
        import numpy as np

        now = time.monotonic()
        overlay = SimpleNamespace(resolutionAzimuth=16, resolutionElevation=8, polarizationVisible=False, publish_markers=Mock(), publish_spatial_intensity=Mock())
        state = SimpleNamespace(
            controller=SimpleNamespace(busy=False),
            advertisers=SimpleNamespace(selected={"public:a"}, refresh=Mock()),
            _last_list_update=now,
            appconfig=SimpleNamespace(get=lambda *args: 1.0),
            overlay=overlay,
            _spectra={("public:a", 0): (now, np.full((16, 8), 2.0), [])},
            _polarizations={},
        )
        for age, expected in ((0, 1), (0.08, 1), (0.19, 1), (0.6, 0.5), (1, 0)):
            with patch("demos.ble.camera.main.time.monotonic", return_value=now + age):
                BLECamera.updateSpatialSpectrum(state)
            np.testing.assert_allclose(overlay.publish_spatial_intensity.call_args.args[0], expected, atol=1e-8)
        # Freshness does not hold an old direction when a new measurement arrives.
        moved = np.zeros((16, 8))
        moved[12, 6] = 1
        state._spectra = {("public:a", 0): (now + 1, moved, [])}
        with patch("demos.ble.camera.main.time.monotonic", return_value=now + 1):
            BLECamera.updateSpatialSpectrum(state)
        np.testing.assert_array_equal(overlay.publish_spatial_intensity.call_args.args[0], moved)

    def test_combined_camera_exposes_and_marks_the_common_direction_once(self):
        import numpy as np
        from demos.ble.common.beamforming import combine_board_spectra

        records = []
        for board in range(4):
            power = np.zeros((16, 8))
            power[8, 5] = 0.8
            power[4 + 2 * board, 3] = 1
            records.append((10, power, []))
        image, paths = combine_board_spectra(records, 10, 1)
        np.testing.assert_allclose(paths, [[1 / 16, 3 / 8]])
        self.assertEqual(image.max(), 1)
        self.assertEqual(np.count_nonzero(image == 1), 1)

    def test_centered_peak_and_unresolved_single_baseline(self):
        import numpy as np
        from demos.ble.common.beamforming import combine_board_spectra

        power = np.zeros((16, 8))
        power[7:9, 3:5] = 1
        _, paths = combine_board_spectra([(10, power, [])], 10, 1)
        np.testing.assert_allclose(paths, [[0, 0]])
        power[7:9, :] = 1
        _, paths = combine_board_spectra([(10, power, [])], 10, 1)
        self.assertEqual(paths, [])

    def test_polarization_survives_short_coverage_gap_but_expires(self):
        import numpy as np

        now = time.monotonic()
        config = SimpleNamespace(get=lambda section, key: 0.25 if section == "beamformer" else 1.0)
        overlay = SimpleNamespace(resolutionAzimuth=16, resolutionElevation=8, polarizationVisible=True, publish_markers=Mock(), publish_spatial_intensity=Mock(), publish_polarization=Mock())
        state = SimpleNamespace(
            controller=SimpleNamespace(busy=False),
            advertisers=SimpleNamespace(selected={"public:a"}),
            _measurement_after=now - 1,
            _last_list_update=now,
            appconfig=config,
            overlay=overlay,
            _spectra={},
            _polarizations={},
            _polarization_field=None,
        )
        jones = np.broadcast_to(np.array([1, 1j]) / np.sqrt(2), (16, 8, 2))
        power = np.ones((16, 8))
        BLECamera._spectrum(state, ("public:a", 0), (power, [[0, 0]], jones), now)
        BLECamera._spectrum(state, ("public:a", 0), (power, [[0, 0]], None), now + 0.05)
        self.assertEqual(state._polarizations[("public:a", 0)][0], now)
        with patch("demos.ble.camera.main.time.monotonic", return_value=now + 0.1):
            BLECamera.updateSpatialSpectrum(state)
        vertical, horizontal = overlay.publish_polarization.call_args.args
        np.testing.assert_allclose(abs(vertical), 1 / np.sqrt(2))
        with patch("demos.ble.camera.main.time.monotonic", return_value=now + 0.3):
            BLECamera.updateSpatialSpectrum(state)
        vertical, horizontal = overlay.publish_polarization.call_args.args
        np.testing.assert_array_equal(vertical, 0)
        np.testing.assert_array_equal(horizontal, 0)

    def test_calibration_error_only_removes_the_affected_board(self):
        keys = [("public:a", board) for board in (0, 1)]
        state = SimpleNamespace(_spectra=dict.fromkeys(keys), _polarizations=dict.fromkeys(keys), pool=SimpleNamespace(hosts=["left", "right"]), controller=Mock())
        BLECamera._receiver_error(state, 1, "calibration expired")
        self.assertEqual(list(state._spectra), keys[:1])
        self.assertEqual(list(state._polarizations), keys[:1])
        state.controller.report_error.assert_called_once_with("right: calibration expired")

    def test_resolution_change_discards_old_spectra(self):
        import numpy as np

        now = time.monotonic()
        overlay = SimpleNamespace(resolutionAzimuth=96, resolutionElevation=64, polarizationVisible=False, publish_markers=Mock(), publish_spatial_intensity=Mock())
        state = SimpleNamespace(
            _last_list_update=now,
            appconfig=SimpleNamespace(get=lambda *args: 60),
            advertisers=SimpleNamespace(selected={"source"}),
            overlay=overlay,
            controller=SimpleNamespace(busy=False),
            _spectra={("source", 0): (now, np.ones((128, 96)), [])},
            _polarizations={("source", 0): None},
            _measurement_after=now - 1,
        )
        BLECamera.updateSpatialSpectrum(state)
        self.assertFalse(state._spectra)
        image = overlay.publish_spatial_intensity.call_args.args[0]
        self.assertEqual(image.shape, (96, 64))
        self.assertFalse(image.any())
        BLECamera._spectrum(state, ("source", 0), (np.ones((128, 96)), [], None), now)
        self.assertFalse(state._spectra)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(QtWidgets is None, "Install the optional PyQt6 demo dependencies")
def test_instantaneous_csi_rejects_reference_only_and_expires_curves():
    from importlib import import_module
    import numpy as np
    import pytest

    QtCharts = pytest.importorskip("PyQt6.QtCharts")
    from espargos.ble_iq import BLEChannelResponse

    demo = import_module("demos.ble.instantaneous-csi.main").BLEChannelApplication
    qt = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    h = np.ones((1, 2, 4, 3), complex)
    quality = np.zeros(h.shape)
    quality[0, 0, 0] = 1
    response = BLEChannelResponse(np.array([-1e5, 0, 1e5]), h, quality, np.ones(h.shape[:-1]))
    app = SimpleNamespace(controller=SimpleNamespace(receiver=Mock()), appconfig=ConfigManager(demo.DEFAULT_CONFIG), _valid_bins=demo._valid_bins, boardCount=2, _plot_references={}, _after=0, _received_at=time.monotonic())
    app.controller.receiver.channel_response.return_value = response
    cluster = SimpleNamespace(board_index=0, rf_switch_state=np.full((1, 2, 4), 2))
    assert demo.process_cluster(app, cluster) is None
    app.appconfig.set({"min_antennas": 3})
    quality[0, 1, 1] = 1
    quality[0, 0, 2, 0] = 1
    assert demo.process_cluster(app, cluster) is None
    quality[0, 0, 2, 1] = 1
    assert demo.process_cluster(app, cluster) is not None
    quality[0, 0, 2] = 0
    app.appconfig.set({"min_antennas": 2})
    from collections import deque

    app._history = deque()
    app._trim_history = lambda: demo._trim_history(app)
    result = demo.process_cluster(app, cluster)
    board, reference, routes, frequencies, ratios = result
    second = ratios.copy()
    second[5] *= 3j
    now = time.monotonic()
    app._history.extend([(now - 0.1, result), (now, (board, reference, routes, frequencies, second))])
    power, phase = ([QtCharts.QLineSeries() for _ in range(8 * app.boardCount)] for _ in range(2))
    axis = QtCharts.QValueAxis()
    demo.updateCurves(app, power, phase, axis)
    assert [s.count() for s in phase] == [3, 0, 0, 0, 0, 3, 0, 0] + [0] * 8
    assert power[5].at(0).y() == pytest.approx(10 * np.log10(5))
    assert phase[5].at(0).y() == pytest.approx(np.pi / 4)
    # Measurements on the opposite feed must not pull the displayed ratio.
    other_routes = routes.copy()
    other_routes[5] = 3
    app._history.appendleft((now - 0.2, (board, reference, other_routes, frequencies, second * 10)))
    demo.updateCurves(app, power, phase, axis)
    assert power[5].at(0).y() == pytest.approx(10 * np.log10(5))
    # An independent reference on board two contributes its own eight curves.
    app._history.append((now + 0.001, (1, reference, routes, frequencies, ratios)))
    demo.updateCurves(app, power, phase, axis)
    assert power[13].at(0).y() == pytest.approx(0)
    assert power[5].at(0).y() == pytest.approx(10 * np.log10(5))
    app._history.pop()
    app.appconfig.set({"backlog": {"size": 1}})
    demo.updateCurves(app, power, phase, axis)
    assert phase[5].at(0).y() == pytest.approx(np.pi / 2)
    app._history[0] = (now - 2, app._history[0][1])
    demo.updateCurves(app, power, phase, axis)
    assert all(s.count() == 0 for s in power + phase)


@unittest.skipIf(QtWidgets is None, "Install the optional PyQt6 demo dependencies")
def test_ble_chart_reference_survives_alignment_changes_and_temporary_loss():
    from importlib import import_module
    import numpy as np
    import pytest

    pytest.importorskip("PyQt6.QtCharts")
    from espargos.ble_iq import BLEChannelResponse

    module = import_module("demos.ble.instantaneous-csi.main")
    demo = module.BLEChannelApplication
    receiver = Mock()
    app = SimpleNamespace(controller=SimpleNamespace(receiver=receiver), appconfig=ConfigManager(demo.DEFAULT_CONFIG), _valid_bins=demo._valid_bins, boardCount=1, _plot_references={}, _after=0)
    field = np.array([1, 2j, -0.5, 0.3j, 0.2, -0.1, 0.8j, 0.5])[:, None] * np.ones((8, 3))

    def packet(stamp, anchor, missing=()):
        values = (field / field[anchor]).copy()
        for sensor in missing:
            values[sensor] = np.nan
        response = values.reshape(1, 2, 4, 3)
        receiver.channel_response.return_value = BLEChannelResponse(np.arange(3), response, np.ones(response.shape), np.ones((1, 2, 4)))
        with patch.object(module.time, "monotonic", return_value=stamp):
            result = demo.process_cluster(app, SimpleNamespace(board_index=0, rf_switch_state=np.full((1, 2, 4), 2)))
            return None if result is None else result[-1]

    first = packet(1.0, 0)
    changed = packet(1.1, 1)
    np.testing.assert_allclose(changed, first)
    assert packet(1.2, 1, missing=(0,)) is None
    switched = packet(1.4, 1, missing=(0,))
    np.testing.assert_allclose(switched.reshape(8, 3)[1:], (field / field[1])[1:])
    recovered = packet(1.45, 0)
    np.testing.assert_allclose(recovered.reshape(8, 3), field / field[1])
