"""Qt-only camera models. No device or video camera is required."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from PyQt6 import QtWidgets
    from demos.ble.common.advertisers import Advertisers
    from demos.ble.camera.main import BLECamera, BLEOverlay
    from demos.ble.common.controller import BLEController
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
        overlay.publish_spatial_intensity(spectrum_intensity(power)*.5)
        image, _ = overlay._images.requestImage("spatial", None)
        self.assertLess(image.pixelColor(0, 0).green(), initial / 2)
        self.assertEqual((image.width(), image.height()), (16, 8))

    def test_fresh_spectrum_is_steady_and_stale_spectrum_fades_after_contrast(self):
        import numpy as np
        now = time.monotonic()
        overlay = SimpleNamespace(resolutionAzimuth=16,resolutionElevation=8,polarizationVisible=False,
            publish_markers=Mock(),publish_spatial_intensity=Mock())
        state = SimpleNamespace(controller=SimpleNamespace(busy=False),
            advertisers=SimpleNamespace(selected={"public:a"},refresh=Mock()),
            _last_list_update=now,appconfig=SimpleNamespace(get=lambda *args:1.),overlay=overlay,
            _spectra={"public:a":(now,np.full((16,8),2.))},_directions={},_polarizations={})
        for age, expected in ((0,1),(.08,1),(.19,1),(.6,.5),(1,0)):
            with patch("demos.ble.camera.main.time.monotonic",return_value=now+age):
                BLECamera.updateSpatialSpectrum(state)
            np.testing.assert_allclose(overlay.publish_spatial_intensity.call_args.args[0],expected,atol=1e-8)
        # Freshness does not hold an old direction when a new measurement arrives.
        moved = np.zeros((16,8))
        moved[12,6] = 1
        state._spectra = {"public:a":(now+1,moved)}
        with patch("demos.ble.camera.main.time.monotonic",return_value=now+1):
            BLECamera.updateSpatialSpectrum(state)
        np.testing.assert_array_equal(overlay.publish_spatial_intensity.call_args.args[0],moved)

    def test_polarization_survives_short_coverage_gap_but_expires(self):
        import numpy as np
        now = time.monotonic()
        config = SimpleNamespace(get=lambda section,key: .25 if section == "beamformer" else 1.)
        overlay = SimpleNamespace(resolutionAzimuth=16,resolutionElevation=8,polarizationVisible=True,
            publish_markers=Mock(),publish_spatial_intensity=Mock(),publish_polarization=Mock())
        state = SimpleNamespace(controller=SimpleNamespace(busy=False),advertisers=SimpleNamespace(selected={"public:a"}),
            _measurement_after=now-1,_last_list_update=now,appconfig=config,overlay=overlay,
            _spectra={},_directions={},_polarizations={},_polarization_field=None)
        jones = np.broadcast_to(np.array([1,1j])/np.sqrt(2),(16,8,2))
        power = np.ones((16,8))
        BLECamera._spectrum(state,"public:a",(power,[[0,0]],jones),now)
        BLECamera._spectrum(state,"public:a",(power,[[0,0]],None),now+.05)
        self.assertEqual(state._polarizations["public:a"][0],now)
        with patch("demos.ble.camera.main.time.monotonic",return_value=now+.1):
            BLECamera.updateSpatialSpectrum(state)
        vertical,horizontal = overlay.publish_polarization.call_args.args
        np.testing.assert_allclose(abs(vertical),1/np.sqrt(2))
        with patch("demos.ble.camera.main.time.monotonic",return_value=now+.3):
            BLECamera.updateSpatialSpectrum(state)
        vertical,horizontal = overlay.publish_polarization.call_args.args
        np.testing.assert_array_equal(vertical,0)
        np.testing.assert_array_equal(horizontal,0)

    def test_late_receiver_action_after_shutdown(self):
        executor = ThreadPoolExecutor(max_workers=1)
        executor.shutdown()
        action = Mock()
        closing = SimpleNamespace(_closed=True, _executor=executor)
        BLEController._submit(closing, action, "Late receiver action")
        action.assert_not_called()

    def test_resolution_change_discards_old_spectra(self):
        import numpy as np
        now = time.monotonic()
        overlay = SimpleNamespace(resolutionAzimuth=96, resolutionElevation=64,
            polarizationVisible=False, publish_markers=Mock(), publish_spatial_intensity=Mock())
        state = SimpleNamespace(_last_list_update=now, appconfig=SimpleNamespace(get=lambda *args: 60),
            advertisers=SimpleNamespace(selected={'source'}), overlay=overlay,
            controller=SimpleNamespace(busy=False), _spectra={'source': (now, np.ones((128, 96)))},
            _directions={'source': [[0, 0]]}, _polarizations={'source': None}, _measurement_after=now-1)
        BLECamera.updateSpatialSpectrum(state)
        self.assertFalse(state._spectra)
        image = overlay.publish_spatial_intensity.call_args.args[0]
        self.assertEqual(image.shape, (96, 64))
        self.assertFalse(image.any())
        BLECamera._spectrum(state, 'source', (np.ones((128, 96)), [], None), now)
        self.assertFalse(state._spectra)


if __name__ == "__main__":
    unittest.main()
