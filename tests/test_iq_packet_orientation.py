import unittest

import numpy as np

from espargos.iq_packet import IQChunkPacket


class IQPacketOrientationTests(unittest.TestCase):
    def test_dump_q_polarity_is_converted_to_conventional_complex_baseband(self):
        packet = IQChunkPacket.__new__(IQChunkPacket)
        # Hardware dump word: I=+3, raw Q=+4. Raw Q has the opposite polarity
        # to conventional complex baseband, so the decoded sample is 3-4j.
        packet.samples = np.asarray([3 | (4 << 10)], dtype=np.uint32)
        np.testing.assert_array_equal(packet.decode_iq(), np.asarray([3 - 4j], dtype=np.complex64))

    def test_negative_raw_q_is_sign_extended_before_orientation_conversion(self):
        packet = IQChunkPacket.__new__(IQChunkPacket)
        raw_q_minus_four = (-4) & 0x3FF
        packet.samples = np.asarray([3 | (raw_q_minus_four << 10)], dtype=np.uint32)
        np.testing.assert_array_equal(packet.decode_iq(), np.asarray([3 + 4j], dtype=np.complex64))

    def test_gain_index_ignores_alternate_table_bank_bit(self):
        packet = IQChunkPacket.__new__(IQChunkPacket)
        packet.samples = np.asarray([60 << 20, (0x80 | 60) << 20], dtype=np.uint32)
        np.testing.assert_array_equal(packet.sample_rx_gain(), np.asarray([60, 60], dtype=np.uint8))


if __name__ == "__main__":
    unittest.main()
