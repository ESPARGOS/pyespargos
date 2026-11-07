"""Waveform repetition, channel separation and off-bin targets at every RX rate."""

import unittest
import numpy as np
from test_mimo_processing import radar

POSITIONS = [(x * 0.06, y * 0.06) for y in range(2) for x in range(4)]


class SampleRateTests(unittest.TestCase):
    def test_edge_guards_and_processing_masks_at_every_rate(self):
        for rate in radar.SAMPLE_RATES:
            with self.subTest(rate=rate):
                p = radar.MIMOProcessor(POSITIONS, sample_rate=rate)
                for tx, waveform in enumerate(radar.pilots(rate)):
                    samples = waveform.synthesize(samples=p.tx_samples)
                    frequencies = np.fft.fftfreq(len(samples), 1 / radar.RATE)
                    power = abs(np.fft.fft(samples)) ** 2
                    # Leave 1/32 of the RX rate between each outer tone and
                    # the band edge, allowing only DAC quantization noise.
                    guard = abs(frequencies) > rate * 15 / 32 + 1
                    self.assertLess(power[guard].sum() / power.sum(), 1e-5)
                    occupied = abs(p.pilots[tx]) > abs(p.pilots[tx]).max() * 0.01
                    occupied[0] = False  # Processing deliberately discards DC.
                    np.testing.assert_array_equal(p.masks[tx], occupied)

    def test_waveforms_repeat_without_aliasing(self):
        for rate, length in zip(radar.SAMPLE_RATES, [256, 512, 1024, 2048, 4096, 8192]):
            with self.subTest(rate=rate):
                sampling = radar.Sampling(rate)
                decimation, stride = sampling.sample_step, sampling.bin_stride
                self.assertEqual(sampling.tx_samples, length)
                for waveform in radar.pilots(rate):
                    samples = waveform.synthesize(samples=length)
                    # Every accumulation must see exactly the same waveform,
                    # including when RX vectors cross a TX buffer boundary.
                    indices = np.arange(256 * 31) * decimation % length
                    received = samples[indices].reshape(-1, 256)
                    np.testing.assert_array_equal(received, np.broadcast_to(received[0], received.shape))
                    frequencies = np.fft.fftfreq(length, 1 / radar.RATE)
                    power = abs(np.fft.fft(samples)) ** 2
                    # Integer DAC quantization is the only out-of-band energy.
                    self.assertLess(power[abs(frequencies) > rate / 2].sum() / power.sum(), 1e-5)
                    spectrum = np.fft.fft(received[0])
                    bins = np.rint(np.fft.fftfreq(256) * 256).astype(int)
                    self.assertLess(np.sum(abs(spectrum[bins % stride != 0]) ** 2) / np.sum(abs(spectrum) ** 2), 1e-5)

    def test_raw_iq_off_bin_targets_at_every_rate(self):
        for rate in radar.SAMPLE_RATES:
            p = radar.MIMOProcessor(POSITIONS, sample_rate=rate)
            scale = radar.RATE / rate
            for distance, velocity in [(0.9375 * scale, 0.37), (6.0625 * scale, -0.42)]:
                with self.subTest(rate=rate, distance=distance):
                    p.reset()
                    delay = np.exp(-2j * np.pi * p.offsets * 2 * distance / radar.C)
                    for t in np.arange(p.history) * p.dt:
                        channel = 1 + 0.05 * delay * np.exp(-2j * np.pi * 2 * p.frequency * velocity * t / radar.C)
                        spectrum = p.pilots.sum(axis=0) * channel
                        iq = np.broadcast_to(np.fft.ifft(spectrum), (p.receiver_count, 256))
                        p.frames.append(p.channels(iq, np.ones_like(iq)))
                    rd, ra = p.maps()
                    r, d = np.unravel_index(rd.argmax(), rd.shape)
                    self.assertLessEqual(abs(p.ranges[r] - distance), 0.0625 * scale + 1e-6)
                    self.assertLessEqual(abs(p.velocities[d] - velocity), abs(np.diff(p.velocities)[0]) / 2)
                    self.assertLessEqual(abs(p.ranges[ra.max(axis=1).argmax()] - distance), 0.0625 * scale + 1e-6)

    def test_two_targets_between_bins_at_every_rate(self):
        for rate in radar.SAMPLE_RATES:
            with self.subTest(rate=rate):
                p = radar.MIMOProcessor(POSITIONS, sample_rate=rate)
                scale = radar.RATE / rate
                targets = [(0.9375 * scale, 0.37, 0.05), (6.0625 * scale, -0.42, 0.03)]
                rng = np.random.default_rng(42)
                for t in np.arange(p.history) * p.dt:
                    channel = np.ones(256, complex)
                    for distance, velocity, amplitude in targets:
                        channel += amplitude * np.exp(-2j * np.pi * (p.offsets * 2 * distance / radar.C + 2 * p.frequency * velocity * t / radar.C))
                    iq = np.tile(np.fft.ifft(p.pilots.sum(axis=0) * channel), (p.receiver_count, 1))
                    iq += 0.0001 * (rng.normal(size=iq.shape) + 1j * rng.normal(size=iq.shape))
                    p.frames.append(p.channels(iq, np.ones_like(iq)))
                rd, _ = p.maps()
                for distance, velocity, _ in targets:
                    velocities = abs(p.velocities - velocity) < 0.08
                    r, d = np.unravel_index(rd[:, velocities].argmax(), rd[:, velocities].shape)
                    self.assertLessEqual(abs(p.ranges[r] - distance), 0.1875 * scale)
                    self.assertLessEqual(abs(p.velocities[velocities][d] - velocity), abs(np.diff(p.velocities)[0]) / 2)

    def test_counter_wrap_and_missing_windows_at_every_rate(self):
        for rate in radar.SAMPLE_RATES:
            with self.subTest(rate=rate):
                p = radar.MIMOProcessor(POSITIONS, sample_rate=rate)
                self.assertGreater(1 / p.dt, 170)
                self.assertLess(1 / p.dt, 180)
                first = radar.COUNTER_MODULUS - p.interval_chunks
                frame = np.zeros((p.path_count, 256), np.complex64)
                for i in [0, 1, 3]:
                    self.assertTrue(p.push(frame, (first + i * p.interval_chunks) % radar.COUNTER_MODULUS))
                self.assertEqual(p.missing, 1)
                self.assertIsNone(p.frames[2])
                self.assertTrue(p.push(frame, (first + 2 * p.interval_chunks) % radar.COUNTER_MODULUS))
                self.assertEqual(p.missing, 0)
                self.assertFalse(p.push(frame, p.last_index))

    def test_unsupported_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            radar.MIMOProcessor(POSITIONS, sample_rate=12e6)
