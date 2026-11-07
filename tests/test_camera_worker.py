"""The spawned worker accepts plain captures and returns only display data."""

from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import numpy as np
from demos.camera.worker import CaptureProcessor, process_capture
from demos.camera.main import EspargosIQCamera
from demos.camera.processing import peak_directions


def test_spawned_processor_matches_local_processor():
    rng = np.random.default_rng(37)
    r, c = np.indices((2, 4))
    wave = np.exp(1j * np.pi * (c.ravel() * 0.25 - r.ravel() * 0.15))
    tone = 20 * np.exp(2j * np.pi * 32 * np.arange(8 * 256) / 256)
    samples = (rng.normal(size=(8, 8 * 256)) + 1j * rng.normal(size=(8, 8 * 256)) + wave[:, None] * tone).astype(np.complex64).reshape(8, 8, 256)
    config = EspargosIQCamera.DEFAULT_CONFIG
    args = (samples, np.ones((8, 1, 256), np.complex64), (np.hanning(256) / np.hanning(256).sum()).astype(np.float32), (2, 4), 299792458 / (2 * 0.06), 80e6, ("test",), config["beamformer"], config["visualization"], 1.0)
    expected = CaptureProcessor().process(*args)
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        actual = pool.submit(process_capture, *args).result(timeout=20)
        # A second request uses the same processor, with its noise history.
        again = pool.submit(process_capture, *args).result(timeout=20)
    for key in ["rows", "image", "noise"]:
        np.testing.assert_allclose(actual[key], expected[key])
    assert again["rows"].shape == (8, 256, 4)
    assert actual["strongest"] is not None
    np.testing.assert_allclose(peak_directions(actual["image"], actual["image"].max() * 0.5)[0], (0.25, -0.15), atol=0.02)
