"""Exercise the NumPy demodulator with independent GFSK waveforms."""
from pathlib import Path
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


from ble_waveforms import waveform
from espargos.ble_decoder import decode


class DemodulationTest(unittest.TestCase):
    def decode(self, signal, divider=8, phy=0):
        raw = ((signal.real.astype(np.int32) & 1023) | ((signal.imag.astype(np.int32) & 1023) << 10)).astype(np.uint32)
        return decode(raw, divider, 37, phy)

    def test_unknown_address_and_unknown_crc(self):
        for address in (0x12345678, 0xD391AD47):
            for phy in (0, 1):
                for divider in ((1, 2, 4) if phy else (1, 2, 4, 8)):
                    with self.subTest(address=hex(address), phy=phy, divider=divider):
                        signal, pdu = waveform(address, phy=phy, divider=divider)
                        packet = self.decode(signal, divider, phy)
                        self.assertIsNotNone(packet)
                        self.assertEqual(packet.access_address, address)
                        self.assertEqual(packet.flags & 3, 1)
                        self.assertEqual(packet.prefix, pdu)

    def test_advertising_crc(self):
        signal, pdu = waveform(0x8e89bed6)
        packet = self.decode(signal)
        self.assertEqual(packet.flags, 3)
        self.assertEqual(packet.prefix, pdu)
        corrupt, _ = waveform(0x8e89bed6, corrupt=True)
        self.assertIsNone(self.decode(corrupt))

    def test_empty_and_maximum_data_packets(self):
        for phy, divider in ((0, 8), (1, 4)):
            for length in (0, 1, 27, 251, 255):
                signal, pdu = waveform(0x12345678, phy=phy, divider=divider, length=length)
                packet = self.decode(signal, divider, phy)
                self.assertIsNotNone(packet, (phy, length))
                self.assertEqual(packet.prefix, pdu[:32])
                self.assertEqual(packet.flags & 3, 1)

    def test_carrier_offset_and_noise(self):
        for phy, divider in ((0, 8), (1, 4)):
            for cfo in (-100_000, 0, 100_000):
                signal, pdu = waveform(0x8e89bed6, phy=phy, divider=divider, cfo=cfo, noise=.04)
                packet = self.decode(signal, divider, phy)
                self.assertIsNotNone(packet, (phy, cfo))
                self.assertEqual(packet.prefix, pdu)

    def test_receiver_dc_offset(self):
        for phy, divider in ((0, 8), (1, 4)):
            signal, pdu = waveform(0x8e89bed6, phy=phy, divider=divider)
            packet = self.decode(signal + 180 + 70j, divider, phy)
            self.assertIsNotNone(packet)
            self.assertEqual(packet.flags & 3, 3)
            self.assertEqual(packet.prefix, pdu)

    def test_preamble_search_is_bounded(self):
        signal, _ = waveform(0x12345678)
        self.assertIsNone(self.decode(np.r_[np.zeros(80 * 4), signal]))

    def test_unknown_packet_length_must_match_the_burst(self):
        signal, _ = waveform(0x12345678)
        # A false header inside a longer burst cannot qualify a short packet.
        self.assertIsNone(self.decode(np.r_[signal, np.zeros(80 * 4)]))

    def test_noise_and_truncated_packet(self):
        rng = np.random.default_rng(72)
        for _ in range(30):
            noise = 50 * (rng.normal(size=16384) + 1j * rng.normal(size=16384))
            self.assertIsNone(self.decode(noise))
        signal, _ = waveform(0x8e89bed6)
        self.assertIsNone(self.decode(signal[:300]))


if __name__ == '__main__':
    unittest.main()
