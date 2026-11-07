"""Every captured FFT row survives GUI refresh coalescing."""

import threading
from types import SimpleNamespace
import numpy as np
from demos.camera.main import EspargosIQCamera, CHUNK_SAMPLES
from demos.iq.common.iq_waterfall import WaterfallImageProvider, power_db_row
from demos.camera.processing import channelize
from demos.camera.worker import CaptureProcessor


def test_capture_rows_are_preserved_before_gui_refresh_and_during_warmup():
    class Config:
        def get(self, *keys):
            value = EspargosIQCamera.DEFAULT_CONFIG
            for key in keys:
                value = value[key]
            return value

    processor = CaptureProcessor()
    executor = SimpleNamespace(submit=lambda _fn, *args: SimpleNamespace(result=lambda: processor.process(*args)))
    window = (np.hanning(256) / np.hanning(256).sum()).astype(np.float32)
    app = SimpleNamespace(
        _dsp_executor=executor,
        receiver=SimpleNamespace(calibrating=False, calibration_applies_to=lambda _: True, calibration=object(), cal_correction=lambda _: np.ones((1, 2, 4, 256))),
        n_rows=2,
        n_cols=4,
        _combine=lambda x: x[0],
        _correction_source=None,
        _steering_key=None,
        _noise_key=None,
        _lock=threading.Lock(),
        appconfig=Config(),
        _window=window,
        waterfall=WaterfallImageProvider(256, 180),
    )
    EspargosIQCamera._reset_image(app)
    rng = np.random.default_rng(83)
    rows = []
    for _ in range(4):
        iq = (rng.normal(size=(1, 2, 4, 56 * 256)) + 1j * rng.normal(size=(1, 2, 4, 56 * 256))).astype(np.complex64)
        source = SimpleNamespace(iq=iq, sample_rx_gain=0, sample_rate_hz=80e6, center_freq_hz=2472e6, config_generation=np.zeros((1, 2, 4)))
        spectra = channelize(iq.reshape(8, 56, 256), window)
        power = np.mean(abs(spectra) ** 2, axis=0)
        rows.extend(power_db_row(10 * np.log10(np.maximum(power, 1e-12)), CHUNK_SAMPLES))
        EspargosIQCamera._locate_emissions(app, source)
    assert app._waterfall_dirty
    assert np.isclose(app._waterfall_row_us, 3.2)
    np.testing.assert_array_equal(app.waterfall.data, np.asarray(rows)[-180:][::-1])
