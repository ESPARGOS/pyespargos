import unittest
from unittest.mock import Mock, patch

import numpy as np

from espargos.control_legacy import LegacyControllerResources
from espargos.iq_transmitter import IQTransmitter
from espargos.iq_tx_control import _SensorTXError


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tx = object.__new__(IQTransmitter)
        self.board = Mock(host="array")
        self.board._resources = LegacyControllerResources(self.board.control)
        self.tx.boards = [self.board]
        self.tx.states = [{"sensors": [{"boot_id": 42}] * 8}]
        self.tx.uploaded = {}
        self.tx._wait_generation = Mock()
        self.words = np.zeros(1024, dtype="<u4")
        self.antennas = [2, 3]

    def upload(self):
        self.tx._upload(0, 0, self.words, self.antennas)

    def test_lost_chunk_restarts_transfer_and_only_then_caches(self):
        self.tx._wait_generation.side_effect = [_SensorTXError(self.board, 2, 0x103), None]
        with patch("espargos.iq_transmitter.secrets.randbits", side_effect=[101, 102]):
            self.upload()
            self.upload()
        self.assertEqual(self.board.control.fetch.call_count, 4)
        self.assertEqual([c.args[1] for c in self.tx._wait_generation.call_args_list], [101, 102])

    def test_memory_error_is_not_retried_or_cached(self):
        self.tx._wait_generation.side_effect = _SensorTXError(self.board, 2, 0x101)
        with self.assertRaises(_SensorTXError):
            self.upload()
        self.assertEqual(self.board.control.fetch.call_count, 2)
        self.assertEqual(self.tx.uploaded, {})

    def test_interrupted_http_transfer_restarts_before_caching(self):
        self.board.control.fetch.side_effect = [None, ConnectionResetError(), None, None]
        with patch("espargos.iq_transmitter.secrets.randbits", side_effect=[101, 102]):
            self.upload()
        self.assertEqual(self.board.control.fetch.call_count, 4)
        self.tx._wait_generation.assert_called_once_with(self.board, 102, self.antennas)
        self.assertEqual(set(self.tx.uploaded), {(0, a, 0) for a in self.antennas})
