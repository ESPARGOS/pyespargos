import unittest
from unittest.mock import Mock, patch
import numpy as np
from espargos.iq_transmitter import IQTransmitter
from espargos.iq_tx_control import _SensorTXError


class UploadTests(unittest.TestCase):
    def session(self):
        s = object.__new__(IQTransmitter)
        s.file_sources = {}
        s.uploaded = {}
        s.boards = [Mock(host="array")]
        s.states = [{"sensors": [{"boot_id": 42}] * 8}]
        s.wait_generation = Mock()
        return s

    def test_lost_chunk_restarts_transfer_and_only_then_caches(self):
        s = self.session()
        s.wait_generation.side_effect = [_SensorTXError(s.boards[0], 2, 0x103), None]
        with patch("espargos.iq_transmitter.Path.stat", return_value=Mock(st_mtime_ns=1, st_size=4096)), patch("espargos.iq_transmitter.load_iq", return_value=np.zeros(1024, dtype="<u4")), patch(
            "espargos.iq_transmitter.secrets.randbits", side_effect=[101, 102]
        ):
            s.upload(0, 0, "wave.npy", 1024, list(range(8)))
            self.assertEqual(s.boards[0].control.fetch.call_count, 4)
            self.assertEqual([c.args[1] for c in s.wait_generation.call_args_list], [101, 102])
            s.upload(0, 0, "wave.npy", 1024, list(range(8)))
            self.assertEqual(s.boards[0].control.fetch.call_count, 4)

    def test_memory_error_is_not_retried_or_cached(self):
        s = self.session()
        s.wait_generation.side_effect = _SensorTXError(s.boards[0], 2, 0x101)
        with patch("espargos.iq_transmitter.Path.stat", return_value=Mock(st_mtime_ns=1, st_size=4096)), patch("espargos.iq_transmitter.load_iq", return_value=np.zeros(1024, dtype="<u4")):
            with self.assertRaises(_SensorTXError):
                s.upload(0, 0, "wave.npy", 1024, list(range(8)))
        self.assertEqual(s.boards[0].control.fetch.call_count, 2)
        self.assertEqual(s.uploaded, {})

    def test_interrupted_http_transfer_restarts_before_caching(self):
        s = self.session()
        s.boards[0].control.fetch.side_effect = [None, ConnectionResetError(), None, None]
        with patch("espargos.iq_transmitter.Path.stat", return_value=Mock(st_mtime_ns=1, st_size=4096)), patch("espargos.iq_transmitter.load_iq", return_value=np.zeros(1024, dtype="<u4")), patch(
            "espargos.iq_transmitter.secrets.randbits", side_effect=[101, 102]
        ):
            s.upload(0, 0, "wave.npy", 1024, list(range(8)))
        self.assertEqual(s.boards[0].control.fetch.call_count, 4)
        s.wait_generation.assert_called_once_with(s.boards[0], 102, list(range(8)))
        self.assertEqual(len(s.uploaded), 1)
