import os
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "demos"))
from PyQt6.QtWidgets import QApplication
from common.iq_tx_controller import IQTxController
from espargos import Pool


class TransmitBeamUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.control = IQTxController(Pool("localhost"))
        self.control.data.update(connected=True, authorized=True)
        self.control.session = Mock()
        self.control._apply_signals = Mock()
        self.control.pool.configure_monitor = Mock()
        self.control.session.states = []
        self.control.session.authorized.return_value = [True]
        self.control.pool.calibrate = self.control.session.calibrate
        self.control.session.read.return_value = None
        self.control._run = lambda fn, callback=None, **kw: callback(fn()) if callback else fn()

    def tearDown(self):
        self.control.timer.stop()
        self.control.poll.stop()
        self.control.executor.shutdown(wait=True)
        self.control.deleteLater()

    def test_all_tx_connection_sets_modes_only_after_authorization(self):
        c = self.control
        c.all_tx = True
        c._connected = Mock()
        c.session.read.return_value = [{"modes": ["wifi"] * 8}]
        c.session.authorized.return_value = [False]
        with self.assertRaisesRegex(RuntimeError, "TX authorization"):
            c.connectPool()
        c.session.modes.assert_not_called()
        c.session.authorized.return_value = [True]
        c.connectPool()
        c.session.modes.assert_called_once_with([["iq_tx"] * 8])
        c.setMode(0, "wifi")
        self.assertNotIn("modes", c.pending)

    def test_startup_calibrates_reference_then_starts_antenna_r(self):
        c = self.control
        c.all_tx = True
        states = [
            {
                "modes": ["iq_tx"] * 8,
                "sensors": [{"frequency_hz": 2380000000, "rf_gain": {"a": 0, "b": 0}, "bandwidth_mhz": 40, "output": "reference", "samples": 1024}],
                "calibration": {"phase_deg": [0] * 8, "delay_samples": [0] * 8},
            }
        ]
        from espargos import Calibration

        c.session.calibrate.return_value = Calibration(diagnostics={"phase_residual_deg": 0.2})
        c._connected((states, [True]))
        self.assertTrue(c.data["calibrating"])
        self.assertEqual(c.data["output"], "antenna_r")
        c.flush()  # Start the monitor first.
        c.flush()
        c.session.calibrate.assert_called_once_with(tx_method="monitor", path="reference")
        calls = c.session.mock_calls
        self.assertLess(calls.index(unittest.mock.call.calibrate(tx_method="monitor", path="reference")), calls.index(unittest.mock.call.mute(False)))
        self.assertTrue(c.data["playing"])
        self.assertFalse(c.data["calibrating"])

    def test_failed_startup_never_unmutes(self):
        c = self.control
        c.session.calibrate.side_effect = RuntimeError("calibration failed")
        c.pending.update({"calibrate", "start"})
        with self.assertRaisesRegex(RuntimeError, "calibration failed"):
            c.flush()
        self.assertNotIn(unittest.mock.call.mute(False), c.session.mock_calls)

    def test_completion_callback_error_is_reported_without_crashing_qt(self):
        c = self.control
        errors = []
        c.error.connect(errors.append)
        callback = Mock(side_effect=ValueError("bad status response"))
        c.data.update(busy=True, exclusive=True)
        c._done(callback, {}, None)
        self.assertEqual(errors, ["bad status response"])
        self.assertFalse(c.data["busy"])
        self.assertFalse(c.data["exclusive"])

    def test_waveform_drag_only_pans_horizontally(self):
        from PyQt6.QtCore import QPointF
        from common.iq_tx_plot import IQTxPlot

        plot = IQTxPlot()
        plot.setWidth(400)
        plot.setHeight(200)
        plot.scale, plot.start = 4, 50
        press, move = Mock(), Mock()
        press.position.return_value = QPointF(150, 50)
        move.position.return_value = QPointF(100, 150)
        plot.mousePressEvent(press)
        plot.mouseMoveEvent(move)
        self.assertGreater(plot.start, 50)
        self.assertEqual(plot.center, 0)
        self.assertEqual(plot.vertical_scale, 1)
        plot.deleteLater()

    def test_rf_and_amplitude_edits_survive_steering(self):
        c = self.control
        c.setSetting("gain_a", 2)
        c.setBeam(0, "amplitude", 0.2)
        c.steer(0, 0.4, 0.1)
        c.flush()
        self.assertEqual(c.session.settings.call_args.args[0]["rf_gain"]["a"], 2)
        beam = c._apply_signals.call_args.args[0][0][0]
        self.assertEqual(beam["amplitude"], 0.2)
        self.assertEqual(beam["u"], 0.4)

    def test_gain_change_does_not_rebuild_or_validate_waveform(self):
        c = self.control
        c.data["beams"][0].update(type="file", file="")
        c.setSetting("gain_a", 2)
        c.flush()
        c.session.settings.assert_called_once()
        c._apply_signals.assert_not_called()

    def test_locked_gain_limits_and_explicit_unlock(self):
        c = self.control
        c.setSetting("gain_a", 7)
        c.setSetting("gain_b", 7)
        self.assertEqual((c.data["gain_a"], c.data["gain_b"]), (2, 0))
        c.setSetting("unlocked", True)
        c.setSetting("gain_a", 7)
        c.setSetting("gain_b", 7)
        self.assertEqual((c.data["gain_a"], c.data["gain_b"]), (7, 7))
        c.setSetting("unlocked", False)
        self.assertEqual((c.data["gain_a"], c.data["gain_b"]), (2, 0))

    def test_unconfirmed_stop_requires_reconnect(self):
        c = self.control
        c._done(None, None, TimeoutError("controller unavailable"))
        self.assertFalse(c.data["connected"])
        self.assertFalse(c.data["authorized"])
        c._connected = Mock()
        c.connectPool()
        c.session.reconnect.assert_called_once()

    def test_stop_does_not_require_valid_waveform(self):
        c = self.control
        c.setBeam(0, "type", "file")
        c.data["playing"] = True
        c.toggleTx()
        c.flush()
        c.session.mute.assert_called_once_with(True)
        c._apply_signals.assert_not_called()

    def test_start_applies_pending_modes(self):
        c = self.control
        c.setMode(0, "iq_tx")
        c.toggleTx()
        c.flush()  # prioritized stop
        c.flush()
        self.assertEqual(c.session.modes.call_args.args[0][0][0], "iq_tx")
        self.assertEqual(c.session.mute.call_args.args, (False,))

    def test_updates_during_inflight_request_are_bounded_and_latest_wins(self):
        c = self.control
        c.data["busy"] = True
        for i in range(1000):
            c.steer(0, i / 1000, 0)
        c.flush()
        c._apply_signals.assert_not_called()
        self.assertEqual(c.pending, {"waveform"})
        c.data["busy"] = False
        c.flush()
        self.assertEqual(c._apply_signals.call_args.args[0][0][0]["u"], 0.999)

    def test_stop_while_start_in_flight(self):
        c = self.control
        c.starting = True
        c.toggleTx()
        self.assertIn("stop", c.pending)
        self.assertNotIn("start", c.pending)

    def test_stop_keeps_pending_settings(self):
        c = self.control
        c.pending = {"stop", "rf", "waveform"}
        c.flush()
        self.assertEqual(c.pending, {"rf", "waveform"})
        c.session.mute.assert_called_once_with(True)

    def test_apply_failure_attempts_stop(self):
        c = self.control
        c._apply_signals.side_effect = TimeoutError("test timeout")
        c.pending = {"waveform"}
        with self.assertRaises(TimeoutError):
            c.flush()
        c.session.mute.assert_called_with(True)

    def test_closing_after_calibration_failure_still_shuts_down(self):
        c = self.control
        c.closing = True
        c.data["busy"] = True
        error = RuntimeError("calibration failed")
        error.tx_stopped = True
        with patch.object(c, "_shutdown") as shutdown:
            c._done(None, None, error)
        shutdown.assert_called_once()
        self.assertTrue(c.closing)


class AsyncTransmitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_continuous_drag_dispatches_before_release_and_close_waits(self):
        import time
        from PyQt6.QtTest import QTest

        c = IQTxController(Pool("localhost"))
        c.data.update(connected=True, authorized=True)
        session = Mock(closed=False)
        c.session = session
        c._apply_signals = Mock()
        c.pool._tx = session
        session.states = []
        c.steering_cleared = True
        dispatched = []

        def apply(snapshot):
            dispatched.append(snapshot[0][0]["u"])
            time.sleep(0.015)

        c._apply_signals.side_effect = apply
        closed = []
        c.closed.connect(lambda: closed.append(True))
        try:
            for i in range(30):
                c.steer(0, i / 30, 0)
                QTest.qWait(10)
            # A debounce reset on each mouse event would send nothing here.
            self.assertGreater(len(dispatched), 3)
            deadline = time.monotonic() + 2
            while (c.data["busy"] or c.pending) and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertAlmostEqual(dispatched[-1], 29 / 30)
            c.steer(0, 0.5, 0)
            c.flush()
            c.close()
            self.assertFalse(closed)
            while not closed and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertTrue(closed)
            session.close.assert_called_once()
        finally:
            c.timer.stop()
            c.poll.stop()
            c.executor.shutdown(wait=True)
            c.deleteLater()

    def test_worker_timeout_mutes_and_allows_next_edit(self):
        import time
        from PyQt6.QtTest import QTest

        c = IQTxController(Pool("localhost"))
        c.data.update(connected=True, authorized=True, playing=True)
        c.session = Mock()
        c._apply_signals = Mock()
        c.session.states = []
        c.steering_cleared = True
        c._apply_signals.side_effect = [TimeoutError("test timeout"), None]
        errors = []
        c.error.connect(errors.append)
        try:
            c.steer(0, 0.2, 0)
            c.flush()
            deadline = time.monotonic() + 2
            while c.data["busy"] and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertEqual(errors, ["test timeout"])
            self.assertFalse(c.data["playing"])
            self.assertFalse(c.data["exclusive"])
            c.session.mute.assert_called_once_with(True)
            c.steer(0, 0.3, 0)
            c.flush()
            while c.data["busy"] and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertEqual(c._apply_signals.call_count, 2)
            self.assertEqual(c.data["beams"][0]["u"], 0.3)
        finally:
            c.timer.stop()
            c.poll.stop()
            c.executor.shutdown(wait=True)
            c.deleteLater()


if __name__ == "__main__":
    unittest.main()
