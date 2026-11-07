"""Bounded, ordered waterfall display under loss and out-of-order delivery."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch
import numpy as np


@unittest.skipUnless(importlib.util.find_spec("PyQt6"), "requires optional demo dependencies")
class WaterfallTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "demos/iq-signal-analyzer/iq-signal-analyzer.py"
        spec = importlib.util.spec_from_file_location("iq_signal_analyzer", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.app = SimpleNamespace(
            fft_size=1024,
            _win=np.ones(1024),
            _fft_scale=1024,
            display_width=1,
            iqcontrol=None,
            block_acc={},
            block_time={},
            complete_only=False,
            last_display_block=-1,
            _display_config={},
            _display_schedule=None,
            providers=[Mock() for _ in range(8)],
            rows_rendered=[0] * 8,
            waterfallFrameReady=Mock(),
            _display_spectrum=lambda x: x,
            _power_row=lambda x: np.ones(4),
            _placeholder_row=lambda: np.zeros((1, 4), dtype=np.uint8),
        )
        self.chunk = {a: np.ones(256) for a in range(8)}

    def render(self, now, sets=()):
        with patch.object(self.module.time, "monotonic", return_value=now):
            self.module.EspargosSignalAnalyzer._render_block_rows(self.app, sets, "power")

    def test_late_chunks_and_real_missing_sensor(self):
        self.render(0, [(0, self.chunk), (1, self.chunk), *[(i, self.chunk) for i in range(4, 8)]])
        self.render(0.1, [(2, self.chunk), (3, self.chunk)])
        self.assertEqual(self.app.rows_rendered, [0] * 8)
        self.render(0.21)
        self.assertEqual(self.app.rows_rendered, [2] * 8)
        self.assertFalse(self.app.block_acc)
        self.render(0.3, [(i, {a: x for a, x in self.chunk.items() if a != 7}) for i in range(8, 12)])
        self.render(0.51)
        np.testing.assert_array_equal(self.app.providers[7].add_rows.call_args.args[0], np.zeros((1, 1, 4)))
        self.render(0.6, [(i, self.chunk) for i in range(8, 12)])
        self.render(1)
        self.assertEqual(self.app.rows_rendered, [3] * 8)  # late repair cannot duplicate a row

    def test_interval_whole_missing_burst_and_intentional_idle(self):
        self.app._display_config = dict(trigger=dict(mode="interval", interval_chunks=1024, offset_chunks=0, burst_chunks=4))
        self.render(0, [(i, self.chunk) for i in range(4)])
        self.render(0.21)
        self.render(0.3, [(i, self.chunk) for i in range(2048, 2052)])
        self.render(0.51)
        # Exactly one missing scheduled row, not 511 rows of intentional idle.
        self.assertEqual(self.app.rows_rendered, [3] * 8)
        pixels = self.app.providers[0].add_rows.call_args.args[0]
        self.assertEqual(pixels.shape, (2, 1, 4))
        self.assertFalse(pixels[0].any())
        self.assertTrue(pixels[1].any())

    def test_high_rate_loss_stays_bounded(self):
        partial = {a: x for a, x in self.chunk.items() if a != 7}
        for frame in range(100):
            self.render(frame * 0.04, [(frame * 1024 + i, partial) for i in range(4)])
            self.assertLessEqual(len(self.app.block_acc), 7)
        self.assertGreater(self.app.rows_rendered[0], 90)

    def test_first_arrival_keeps_live_cluster_until_deadline(self):
        app = self.app
        app._pending_lock = threading.Lock()
        app._pending_clusters = []
        app._display_epoch = None
        app._display_epoch_time = -1
        app.active_board = 0
        app.receiver = SimpleNamespace(boards=[SimpleNamespace(revision=SimpleNamespace(antenna_id_to_row_col=lambda a: (0, a)))])
        cluster = SimpleNamespace(completion=np.zeros((1, 1, 8), bool), iq=np.ones((1, 1, 8, 256)), flags=np.zeros((1, 1, 8), int), sync_info=np.ones((1, 1, 8), int), sample_rate_hz=np.full((1, 1, 8), 80000000), chunk_index=0)
        cls = self.module.EspargosSignalAnalyzer
        with patch.object(self.module.time, "monotonic", return_value=0):
            cls._on_iq_cluster(app, cluster)
        cluster.completion[:] = True
        with patch.object(self.module.time, "monotonic", return_value=0.1):
            self.assertEqual(cls._drain_sets(app), [])
        with patch.object(self.module.time, "monotonic", return_value=0.21):
            sets = cls._drain_sets(app)
        self.assertEqual(len(sets[0][1]), 8)

    def test_batched_pixels_match_individual_power_and_phase_rows(self):
        cls = self.module.EspargosSignalAnalyzer
        app = self.app
        app.display_width = 256
        app._win = np.hanning(1024)
        app._fft_scale = app._win.sum() * 512
        rng = np.random.default_rng(3)
        windows = rng.normal(size=(2, 8, 1024)) + 1j * rng.normal(size=(2, 8, 1024))
        for mode in ("power", "phase"):
            app.last_display_block = -1
            app.block_acc.clear()
            app.block_time.clear()
            sets = [(b * 4 + k, {a: windows[b, a, k * 256 : (k + 1) * 256] for a in range(8)}) for b in range(2) for k in range(4)]
            with patch.object(self.module.time, "monotonic", return_value=0):
                cls._render_block_rows(app, sets, mode)
            with patch.object(self.module.time, "monotonic", return_value=0.21):
                cls._render_block_rows(app, [], mode)
            for a in range(8):
                expected = []
                for b in range(2):
                    spectrum = cls._display_spectrum(app, windows[b, a].astype(np.complex64))
                    if mode == "phase":
                        reference = cls._display_spectrum(app, windows[b, 0].astype(np.complex64))
                        expected.append(cls._phase_row(app, spectrum * np.conj(reference)))
                    else:
                        expected.append(cls._power_row(app, spectrum))
                np.testing.assert_array_equal(app.providers[a].add_rows.call_args.args[0], expected)
