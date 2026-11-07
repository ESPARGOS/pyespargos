from types import SimpleNamespace
import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtMultimedia import QVideoFrameFormat
from demos.common.camera.video import VideoCamera


def test_resolution_selector_prefers_jpeg_but_explicit_format_wins():
    class Format:
        def __init__(self, pixel):
            self.pixel = pixel

        def pixelFormat(self):
            return self.pixel

        def resolution(self):
            return SimpleNamespace(width=lambda: 1920, height=lambda: 1080)

        def maxFrameRate(self):
            return 60.0

    yuyv = Format(QVideoFrameFormat.PixelFormat.Format_YUYV)
    jpeg = Format(QVideoFrameFormat.PixelFormat.Format_Jpeg)
    camera = SimpleNamespace(cameraDevice=lambda: SimpleNamespace(videoFormats=lambda: [yuyv, jpeg]))
    camera._format_to_string = lambda f: VideoCamera._format_to_string(camera, f)
    assert VideoCamera._find_format(camera, "1920x1080") is jpeg
    assert VideoCamera._find_format(camera, "1920x1080 @ 60.00 FPS (YUYV)") is yuyv
    assert camera._format_to_string(yuyv) != camera._format_to_string(jpeg)
    with pytest.raises(ValueError):
        VideoCamera._find_format(camera, "999x999")
