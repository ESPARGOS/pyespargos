"""Compare Python samples with the actual sensor synthesis code on the host."""

import ctypes as c
import math
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from espargos.waveforms import CW, TwoTone, LFM, Noise, QPSK, IQSamples

ROOT = Path(__file__).resolve().parents[2]


class Recipe(c.Structure):
    _fields_ = (
        [(k, c.c_uint32) for k in ("version", "kind", "samples", "peak_q16", "seed", "participant_mask", "variant")]
        + [("start_bin", c.c_int32), ("stop_bin", c.c_int32)]
        + [(k, c.c_uint32) for k in ("chirp_samples", "pause_samples", "offset_samples", "temporary")]
    )


class SynthesisTest(unittest.TestCase):
    def test_sensor_agreement(self):
        scratch = ROOT / ".cache"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            lib = Path(tmp) / "synthesis.so"
            subprocess.run(["cc", "-shared", "-fPIC", "-O2", "-I" + str(ROOT / "sensor-firmware/main"), str(ROOT / "sensor-firmware/main/iq/iq_tx_synthesis.c"), "-o", str(lib)], check=True)
            generate = c.CDLL(str(lib)).iq_tx_synthesize
            for n in (256, 512, 1024, 2048, 4096, 8192):
                for source in (CW(1.234e6), CW(-2.345e6), TwoTone(-3.1e6, 7.7e6), TwoTone(30e6, -35e6), TwoTone(1e6, 1e6), LFM(), LFM(-9.3e6, 7.7e6, 1e-6, 0.2e-6), Noise(0), Noise(0xFFFFFFFF), QPSK(123)):
                    r = source.recipe(n)
                    recipe = Recipe(
                        version=6,
                        kind={"cw": 1, "lfm": 2, "noise": 3, "two-tone": 6, "qpsk": 7}[r["type"]],
                        samples=n,
                        peak_q16=511 * 65536,
                        seed=r.get("seed", 0),
                        participant_mask=255,
                        start_bin=math.floor(r.get("first_hz", r.get("frequency_hz", r.get("start_hz", 0))) * n / 80e6 + 0.5),
                        stop_bin=math.floor(r.get("second_hz", r.get("stop_hz", 0)) * n / 80e6 + 0.5),
                        chirp_samples=r.get("chirp_samples", 0),
                        pause_samples=r.get("pause_samples", 0),
                        offset_samples=r.get("offset_samples", 0),
                    )
                    words, z = (c.c_uint32 * n)(), (c.c_int32 * (2 * n))()
                    generate(c.byref(recipe), 0, words, z)
                    words = np.asarray(words)
                    i = (words & 1023).astype(int)
                    q = ((words >> 10) & 1023).astype(int)
                    expected = ((i ^ 512) - 512 + 1j * ((q ^ 512) - 512)) / 511
                    np.testing.assert_array_equal(source.synthesize(n), expected, err_msg=f"{source}, n={n}")
                    np.testing.assert_array_equal(IQSamples(expected).synthesize(n), expected)

    def test_invalid_length(self):
        for n in (0, 255, 300, 16384, 256.0):
            with self.assertRaises(ValueError):
                CW().synthesize(n)


if __name__ == "__main__":
    unittest.main()
