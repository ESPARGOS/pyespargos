import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from espargos import Pool


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.pool = Pool("localhost")
        self.pool.open = Mock(return_value=self.pool)
        self.board = SimpleNamespace(host="localhost")
        self.pool._iq = SimpleNamespace(master_board=self.board)

    def test_monitor_does_not_create_transmitter(self):
        with patch("espargos.pool._rpc") as rpc, patch("espargos.reference_monitor.ReferenceMonitor") as capture:
            monitor = self.pool.configure_monitor({"rx_gain": 0})
            self.assertIsNone(self.pool._tx)
            self.assertIs(monitor, capture.return_value)
            self.assertIs(self.pool.configure_monitor({"rx_gain": 1}), monitor)
            capture.assert_called_once_with(self.board)
            self.pool.configure_monitor({"enabled": False})
            monitor.close.assert_called_once()
            self.assertIsNone(self.pool._monitor)
            rpc.assert_called_with(self.board, "set_iq_monitor", {"enabled": False})

    def test_configuration_failure_releases_new_subscription(self):
        with patch("espargos.pool._rpc", side_effect=TimeoutError), patch("espargos.reference_monitor.ReferenceMonitor") as capture:
            with self.assertRaises(TimeoutError):
                self.pool.configure_monitor({})
            capture.return_value.close.assert_called_once()
            self.assertIsNone(self.pool._monitor)

    def test_pool_close_stops_monitor(self):
        self.pool._monitor = Mock(board=self.board)
        monitor = self.pool._monitor
        self.pool._iq = None
        with patch("espargos.pool._rpc") as rpc:
            self.pool.close()
            rpc.assert_called_once_with(self.board, "set_iq_monitor", {"enabled": False})
            monitor.close.assert_called_once()
