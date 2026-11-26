import numpy as np
import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QCoreApplication
from demos.common.config_manager import ConfigManager
from demos.common.camera.overlay import CameraOverlayModel
from demos.iq.camera.main import EspargosIQCamera
from demos.iq.camera.iq_overlay import IQCameraOverlay


def test_iq_overlay_notifies_when_markers_move():
    app = QCoreApplication.instance() or QCoreApplication([])
    cfg = ConfigManager(EspargosIQCamera.DEFAULT_CONFIG)
    overlay = IQCameraOverlay(cfg)
    markers = []
    overlay.markersChanged.connect(lambda: markers.append(overlay.markers))
    overlay.publish_markers([(0.1, 0.1)])
    assert len(markers) == 1
    assert markers[0][0]["x"] < 0.5  # front-view array right is camera left
    assert markers[0][0]["y"] > 0.5  # positive row direction is downward


def test_overlay_refreshes_only_the_published_texture():
    app = QCoreApplication.instance() or QCoreApplication([])
    overlay = CameraOverlayModel(ConfigManager(EspargosIQCamera.DEFAULT_CONFIG))
    spatial, polarization = overlay.spatialImageSource, overlay.polarizationImageSource
    overlay.publish_image("spatial", np.zeros((6, 8, 4), dtype=np.uint8))
    assert overlay.spatialImageSource != spatial
    assert overlay.polarizationImageSource == polarization
    spatial = overlay.spatialImageSource
    overlay.publish_polarization(np.zeros((8, 6)), np.ones((8, 6)))
    assert overlay.spatialImageSource == spatial
    assert overlay.polarizationImageSource != polarization


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
    cfg.set({"visualization": {"edge_markers": False}})
    overlay.publish_markers([(0.1, 0.65)])
    assert overlay.markers == []


def test_delay_checkbox_survives_switch_to_subspace_beamformer():
    from demos.wifi.camera.backend import CSICamera
    from demos.wifi.camera.csi_overlay import CSIOverlay

    app = QCoreApplication.instance() or QCoreApplication([])
    cfg = ConfigManager(CSICamera.DEFAULT_CONFIG)
    cfg.set({"beamformer": {"type": "MUSIC", "colorize_delay": True}})
    overlay = CSIOverlay(cfg)
    previous = overlay.spatialImageSource
    overlay.publish_spatial_spectrum(np.ones((8, 6)))
    assert overlay.spatialImageSource != previous
