import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np

from espargos.ble_packet import BLEPacket, BLE_PACKET_TYPE_HEADER, channel_frequency
from espargos.ble_processing import Waveform, relative
from espargos.array_processing import steering_vectors_2d


def record(
    samples=None,
    *,
    packed=False,
    prefix=None,
    crc=True,
    settings=37 | 2 << 8 | 40 << 16,
    sequence=1,
    generation=3,
    clock_epoch=1,
    receiver_epoch=2,
    time_us=100_000,
    actual_rf=0,
    measured_gain=None,
    gain_reported=False,
):
    if samples is None:
        samples = np.array([-512 - 512j, 511 + 511j, -1 + 1j, 0j] * 32)
    n = len(samples)
    flags = 3 | 64 | (4 if packed else 0) | (0 if crc else 16)
    header = struct.pack(
        "<18I",
        BLE_PACKET_TYPE_HEADER,
        sequence,
        0,
        0,
        0,
        0,
        time_us,
        0,
        clock_epoch,
        receiver_epoch,
        settings,
        flags,
        0,
        4,
        n,
        n,
        generation,
        0,
    )
    if prefix is None:
        prefix = bytes.fromhex("0010050403020100") + b"\x05\x09Test"
    packet_flags = (3 if crc else 0) | (8 if gain_reported else 0)
    if measured_gain is not None:
        packet_flags |= 12 | measured_gain << 8
    meta = struct.pack(
        "<Ii3I4B32s", 8_000_000, 2_000_000, 0x8E89BED6, 0, packet_flags, 0, len(prefix), actual_rf, 0, prefix
    )
    raw = (np.rint(samples.real).astype(np.int32) & 1023) | ((np.rint(samples.imag).astype(np.int32) & 1023) << 10)
    if packed:
        a, b = raw[::2].astype(np.uint32), raw[1::2].astype(np.uint32)
        wire = (
            np.stack([a & 255, a >> 8 & 255, (a >> 16) | ((b & 15) << 4), b >> 4 & 255, b >> 12 & 255], axis=1).astype(np.uint8).tobytes()
        )
    else:
        wire = raw.astype("<u4").tobytes()
    return header + meta + wire


class PacketTests(unittest.TestCase):
    def test_packed_and_unpacked(self):
        a, b = BLEPacket(record()), BLEPacket(record(packed=True))
        np.testing.assert_array_equal(a.samples, b.samples)
        self.assertEqual(a.samples[0], -512 - 512j)
        self.assertEqual(a.advertiser, "00:01:02:03:04:05")
        self.assertEqual(a.name, "Test")
        self.assertTrue(a.is_advertisement)
        self.assertIsNone(BLEPacket(record(crc=False)).advertiser)

    def test_random_input_preserves_requested_and_effective_routes(self):
        for route in (2, 3):
            packet = BLEPacket(record(settings=37 | 4 << 8 | 40 << 16, actual_rf=route))
            self.assertEqual(packet.rf_switch_mode, 4)
            self.assertEqual(packet.rf_switch, route)
        with self.assertRaisesRegex(ValueError, "effective antenna"):
            BLEPacket(record(settings=37 | 4 << 8 | 40 << 16))

    def test_scanner_is_not_advertiser(self):
        p = BLEPacket(record(prefix=b"\x03\x0c" + bytes(range(6)) + bytes(range(6, 12))))
        self.assertEqual(p.advertiser, "0b:0a:09:08:07:06")
        self.assertFalse(p.is_advertisement)
        for kind in (3, 5):
            random_advertiser = BLEPacket(record(prefix=bytes([kind | 128, 12]) + bytes(range(12))))
            random_scanner = BLEPacket(record(prefix=bytes([kind | 64, 12]) + bytes(range(12))))
            self.assertEqual(random_advertiser.address_type, "random")
            self.assertEqual(random_scanner.address_type, "public")

    def test_reject_bad_wire(self):
        for wire in [record()[:-1], b"BLQ5", b"bad!" + record()[4:]]:
            with self.assertRaises(ValueError):
                BLEPacket(wire)
        data = bytearray(record())
        data[93] = 33
        with self.assertRaises(ValueError):
            BLEPacket(data)
        self.assertEqual([channel_frequency(c) for c in (37, 38, 39)], [2402e6, 2426e6, 2480e6])


class PhaseTests(unittest.TestCase):
    def test_phase_and_fractional_alignment(self):
        rng = np.random.default_rng(8)
        phase = np.cumsum(np.convolve(rng.normal(size=1100), np.ones(9) / 9, mode="same") * 0.4)
        z = np.exp(1j * phase)
        delay, rotation = 2.3, 0.7
        shifted = np.interp(np.arange(len(z)) - delay, np.arange(len(z)), z.real) + 1j * np.interp(
            np.arange(len(z)) - delay, np.arange(len(z)), z.imag
        )
        ref = Waveform(z, 300, 700, 4, 0)
        other = Waveform(shifted * np.exp(1j * rotation), 302, 702, 4, 0)
        fit = relative(ref, other, recovery=True)
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit[0], rotation, delta=0.015)
        self.assertGreater(fit[1], 0.99)
        carrier = Waveform(np.ones(1100, complex), 300, 700, 4, 0)
        self.assertIsNone(relative(carrier, carrier, recovery=True))

    def test_physical_array_projection(self):
        grid = steering_vectors_2d(2, 4, 128, 96, frequency_hz=2402e6, pixel_centers=True)
        expected = (87, 40)
        vector = grid[:, :, expected[0], expected[1]]
        spectrum = abs(np.einsum("rcae,rc->ae", grid.conj(), vector)) ** 2
        self.assertEqual(np.unravel_index(np.argmax(spectrum), spectrum.shape), expected)


if __name__ == "__main__":
    unittest.main()
