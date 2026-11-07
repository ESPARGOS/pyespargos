import unittest
from unittest.mock import Mock, patch
from espargos.iq_tx_reciprocity_calibration import _Captures
from espargos.iq_tx_control import _waveform
from espargos.iq_transmitter import IQTransmitter
from espargos.array_geometry import ArrayGeometry


class AcknowledgementTests(unittest.TestCase):
    def test_failed_capture_setup_detaches_existing_subscriptions(self):
        board = Mock(host="array")
        board.subscribe_sensor_messages.side_effect = [17, RuntimeError("subscription failed")]
        with self.assertRaisesRegex(RuntimeError, "subscription failed"):
            _Captures([board])
        board.unsubscribe_sensor_messages.assert_called_once_with(17)

    def test_partial_stream_updates_do_not_trigger_http_polling(self):
        board = Mock(host="array")
        capture = _Captures([board])
        pending = iter(range(8))

        def receive_one(timeout):
            capture.status[board][next(pending)] = {"generation": 42}

        try:
            with patch.object(capture.changed, "wait", side_effect=receive_one), patch("espargos.iq_tx_control._rpc") as rpc:
                result = capture.wait(board, lambda s: all(x["generation"] == 42 for x in s["sensors"]), 5)
                self.assertEqual(len(result["sensors"]), 8)
                rpc.assert_not_called()
        finally:
            capture.close()

    def test_missing_stream_status_is_confirmed_over_http(self):
        board = Mock(host="array")
        capture = _Captures([board])
        state = {"sensors": [{"generation": 42, "error": 0}] * 8}
        try:
            with patch("espargos.iq_tx_control._rpc", return_value=state) as rpc:
                result = capture.wait(board, lambda s: all(x["generation"] == 42 for x in s["sensors"]), 0)
                self.assertEqual(result, state)
                rpc.assert_called_once_with(board, "get_iq_tx")
        finally:
            capture.close()

    def test_unapplied_command_is_not_treated_as_success(self):
        board = Mock(host="array")
        capture = _Captures([board])
        state = {"sensors": [{"generation": 41, "error": 0, "applied_mode": "iq_tx"}] * 8}
        try:
            with patch("espargos.iq_tx_control._rpc", return_value=state):
                with self.assertRaisesRegex(TimeoutError, "generation=41"):
                    capture.wait(board, lambda s: all(x["generation"] == 42 for x in s["sensors"]), 0)
        finally:
            capture.close()

    def test_rx_waveform_retry_waits_for_new_generation(self):
        board = Mock(host="array")
        recipe = {"beams": [{"waveform": {"antennas": [1, 3]}}]}
        state = {"sensors": [{"generation": 2, "error": 0}] * 8}
        calls = []

        def wait(board, applied, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise TimeoutError("lost command")
            self.assertTrue(applied(state))
            return state

        with patch("espargos.iq_tx_control._rpc", side_effect=[{"generation": 1}, {"generation": 2}]) as rpc:
            self.assertIs(_waveform(board, recipe, 10, wait=wait), state)
            self.assertEqual(rpc.call_count, 2)

    def test_scheduled_waveform_timeout_is_not_retried(self):
        board = Mock(host="array")
        recipe = {"antennas": [0], "start_sample": 1024, "epoch": 42}
        with patch("espargos.iq_tx_control._rpc", return_value={"generation": 1}) as rpc:
            with self.assertRaises(TimeoutError):
                _waveform(board, recipe, 10, wait=Mock(side_effect=TimeoutError("missing ack")))
            rpc.assert_called_once()

    def test_sensor_waveform_error_is_not_retried(self):
        board = Mock(host="array")
        state = {"sensors": [{"generation": 1, "error": 257}] * 8}

        def wait(board, applied, timeout):
            applied(state)

        with patch("espargos.iq_tx_control._rpc", return_value={"generation": 1}) as rpc:
            with self.assertRaisesRegex(RuntimeError, "waveform error 257"):
                _waveform(board, {"antennas": [0]}, 10, wait=wait)
            self.assertEqual(rpc.call_count, 1)

    def test_reconnect_mutes_and_discards_stale_acknowledgements(self):
        session = IQTransmitter.__new__(IQTransmitter)
        board = Mock()
        session.boards = [board]
        session.owner = Mock()
        session.capture = _Captures([board])
        session.capture.status[board] = [{"generation": 99}] * 8
        session.uploaded = {"old": "source"}
        session.authorized = Mock(return_value=[True])
        session.mute = Mock()
        session.read = Mock(return_value=[{"modes": ["wifi"] * 8}])
        session.parallel = lambda fn: [fn(0, board)]
        try:
            self.assertEqual(session.reconnect(), session.read.return_value)
            session.mute.assert_called_once_with(True)
            session.owner.reconnect.assert_called_once()
            board.stop.assert_not_called()
            board.start.assert_not_called()
            self.assertEqual(session.capture.status[board], [None] * 8)
            self.assertEqual(session.uploaded, {})
        finally:
            session.capture.close()

    def test_failed_calibration_refreshes_actual_modes(self):
        session = IQTransmitter.__new__(IQTransmitter)
        session.geometry = ArrayGeometry(["localhost"])
        session.receiver = Mock()
        session.receiver.calibrate_tx.side_effect = RuntimeError("calibration failed")
        session.read = Mock()
        with self.assertRaisesRegex(RuntimeError, "calibration failed"):
            session.calibrate()
        session.read.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class WaveformConnectionTests(unittest.TestCase):
    def test_immediate_recipe_recovers_lost_http_response(self):
        board = Mock(host="array")
        recipe = {"antennas": [0]}
        state = {"sensors": [{"generation": 2, "error": 0}]}

        def wait(board, applied, timeout):
            self.assertTrue(applied(state))
            return state

        with patch("espargos.iq_tx_control._rpc", side_effect=[ConnectionResetError(), {"generation": 2}]) as rpc, patch("espargos.iq_tx_control.time.sleep"):
            self.assertIs(_waveform(board, recipe, 10, wait=wait), state)
            self.assertEqual(rpc.call_count, 2)

    def test_scheduled_http_failure_is_not_repeated(self):
        board = Mock(host="array")
        recipe = {"antennas": [0], "start_sample": 1024, "epoch": 1}
        with patch("espargos.iq_tx_control._rpc", side_effect=ConnectionResetError()) as rpc:
            with self.assertRaises(ConnectionResetError):
                _waveform(board, recipe, 10)
            rpc.assert_called_once()


class ReplayPreparationTests(unittest.TestCase):
    def test_empty_or_inactive_replay_is_prepared_before_first_source(self):
        from contextlib import contextmanager
        from espargos.waveforms import CW
        from espargos.tx_signal import TXSignals

        for samples, enabled, want in [(0, False, True), (1024, False, True), (1024, True, False), (256, True, True)]:
            s = IQTransmitter.__new__(IQTransmitter)
            b = Mock()
            b.host = "board"
            s.boards = [b]
            s.positions = None
            s.capture = Mock()
            s.closed = False
            s.signals = TXSignals(s)
            s.states = [{"modes": ["iq_tx"] * 8, "sensors": [{"samples": samples, "enabled": enabled} for _ in range(8)]}]
            s._parallel = lambda fn: [fn(0, b)]
            seen = []

            @contextmanager
            def edit(board, antennas, modes, resize):
                seen.append(resize)
                yield

            s._edit_replay = edit
            with patch("espargos.iq_transmitter._waveform"):
                signal = s.signals.add(waveform=CW())
            self.assertEqual(seen, [want])
            with patch("espargos.iq_transmitter._waveform"):
                signal.amplitude = 0.2
            self.assertEqual(seen, [want, False])
