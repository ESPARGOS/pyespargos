import importlib.util
from pathlib import Path
import numpy as np
import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QCoreApplication
from demos.common.config_manager import ConfigManager
from demos.common.camera.overlay import CameraOverlayModel
from demos.camera.main import EspargosIQCamera
from demos.camera.iq_overlay import IQCameraOverlay


def test_iq_overlay_inherits_shared_qt_properties_and_notifications():
    app = QCoreApplication.instance() or QCoreApplication([])
    cfg = ConfigManager(EspargosIQCamera.DEFAULT_CONFIG)
    overlay = IQCameraOverlay(cfg)
    assert isinstance(overlay, CameraOverlayModel)
    assert overlay.resolutionAzimuth == 128
    assert overlay.markers == []
    assert not overlay.polarizationVisible
    markers = []
    overlay.markersChanged.connect(lambda: markers.append(overlay.markers))
    overlay.publish_markers([(0.1, 0.1)])
    assert len(markers) == 1
    assert markers[0][0]["x"] < 0.5  # front-view array right is camera left
    assert markers[0][0]["y"] > 0.5  # positive row direction is downward
    assert "+6° / -6°" == markers[0][0]["label"]


def test_csi_overlay_uses_same_shared_projection_state():
    app = QCoreApplication.instance() or QCoreApplication([])
    path = Path(__file__).parents[1] / "demos/wifi/camera/csi_overlay.py"
    spec = importlib.util.spec_from_file_location("camera_csi_overlay_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cfg = ConfigManager(
        {
            "beamformer": {"type": "FFT", "polarization_mode": "show", "resolution_azimuth": 64, "resolution_elevation": 32},
            "visualization": {"space": "camera", "azimuth_correction": 3, "elevation_correction": -4},
            "receiver": {"mac_list_enabled": False},
        }
    )
    overlay = module.CSIOverlay(cfg)
    assert isinstance(overlay, CameraOverlayModel)
    assert overlay.polarizationVisible
    assert overlay.azimuth_correction == 3
    assert overlay.elevation_correction == -4
    assert overlay.markers == []


def test_image_provider_keeps_its_own_rgba_buffer():
    from PyQt6.QtCore import QSize
    from demos.common.camera.images import OverlayImages

    images = OverlayImages()
    pixels = np.zeros((3, 5, 4), dtype=np.uint8)
    pixels[1, 2] = [11, 22, 33, 255]
    images.publish("spatial", pixels)
    pixels[:] = 0
    image, size = images.requestImage("spatial?1", QSize())
    assert (size.width(), size.height()) == (5, 3)
    assert image.pixelColor(2, 1).getRgb() == (11, 22, 33, 255)


@pytest.mark.parametrize("shift,flip", [((0, 0), False), ((12, -8), False), ((-10, 6), True)])
def test_marker_projection_matches_shader_ray_with_alignment(shift, flip):
    app = QCoreApplication.instance() or QCoreApplication([])
    cfg = ConfigManager(EspargosIQCamera.DEFAULT_CONFIG)
    cfg.set({"camera": {"flip_overlay": flip}, "visualization": {"azimuth_correction": shift[0], "elevation_correction": shift[1]}})
    overlay = CameraOverlayModel(cfg)
    pixel = np.array([0.35, 0.62])
    projection = pixel.copy()
    if flip:
        projection[0] = 1 - projection[0]
    ray = np.r_[2 * (projection - 0.5) * np.tan(np.radians([72, 41]) / 2), 1.0]
    ray /= np.linalg.norm(ray)
    a, e = np.radians(shift)
    # Forward transform in spatialspectrum.vert; marker must invert it.
    x, y, z = ray
    y, z = np.cos(e) * y + np.sin(e) * z, -np.sin(e) * y + np.cos(e) * z
    x, z = np.cos(a) * x + np.sin(a) * z, -np.sin(a) * x + np.cos(a) * z
    overlay.publish_markers([(-x, y)])
    np.testing.assert_allclose([overlay.markers[0]["x"], overlay.markers[0]["y"]], pixel, atol=0.001)


def test_out_of_camera_field_signal_gets_an_explicit_edge_marker():
    app = QCoreApplication.instance() or QCoreApplication([])
    cfg = ConfigManager(EspargosIQCamera.DEFAULT_CONFIG)
    overlay = CameraOverlayModel(cfg)
    overlay.publish_markers([(0.1, 0.65)])
    marker = overlay.markers[0]
    assert marker["offscreen"]
    assert marker["y"] == 0.965
    assert marker["label"].startswith("Outside view")
    cfg.set({"visualization": {"edge_markers": False}})
    overlay.publish_markers([(0.1, 0.65)])
    assert overlay.markers == []
