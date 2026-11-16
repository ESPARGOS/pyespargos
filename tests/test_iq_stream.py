"""Exercise stream transport acknowledgements, credit and failure recovery."""
import struct
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import numpy as np
from espargos.calibration import Antenna
from espargos.iq_stream import IQStream
from espargos.stream_sources import psk31_bits


class Device:
    def __init__(self):
        self.now = 0
        self.drop = None
        self.stuck = False
        self.muted = False
        self.received = [bytearray(), bytearray()]
        self.sensors = [dict(available=True, boot_id=10+a, session=0, state="closed", error=0,
                             accepted=0, consumed=0, end=None) for a in range(2)]
        self.board = SimpleNamespace(host="test", control=SimpleNamespace(fetch=self.send))
        tx_sensors = [dict(applied_mode="iq_tx", applied_sample_rate_hz=16000000, samples=1024,
                           boot_id=10+a, frequency_hz=2437000000, enabled=True, sync_seq=7,
                           sample_position=0, epoch=1) for a in range(8)]
        self.tx_state = dict(sensors=tx_sensors, muted=False, sample_rate_hz=16000000)
        self.tx = SimpleNamespace(boards=[self.board], states=[self.tx_state], read=lambda: None,
                                 signals=SimpleNamespace(_antennas=lambda _: [Antenna("test", a) for a in range(2)], _check_beam_calibration=lambda: None))

    def send(self, path, data):
        magic, version, command, mask, reserved, session, offset, count = struct.unpack_from("<IBBBBIII", data)
        assert path == "iq_tx_stream" and magic == 0x31535449 and version == 1 and not reserved
        payload = data[20:]
        for a, s in enumerate(self.sensors):
            if not mask & (1 << a):
                continue
            if command == 1:
                s.update(session=session, state="buffering", accepted=0, consumed=0, end=None)
                self.stride = 2 if struct.unpack_from("<I", payload)[0] == 1 else 1
            elif command == 2:
                if self.drop == (a, offset):
                    self.drop = None
                    continue
                if offset == s["accepted"] and not self.stuck:
                    assert offset + count - s["consumed"] <= IQStream.CAPACITY
                    self.received[a].extend(payload)
                    s["accepted"] += count
            elif command == 3:
                s["state"] = "running"
            elif command == 4:
                assert offset == s["accepted"]
                s["end"] = offset
            elif command == 5:
                s["state"] = "closed"
        return b"sent"

    def rpc(self, board, path, data=None):
        self.now += .025
        if path == "get_iq_tx":
            return self.tx_state
        if path == "set_iq_tx":
            self.muted = data["muted"]
            return {}
        assert path == "get_iq_tx_stream"
        for s in self.sensors:
            if s["state"] == "running":
                s["consumed"] = max(0, s["accepted"] - 2048)
                if s["end"] is not None:
                    s["consumed"] = s["end"]
                    s["state"] = "drained"
        return dict(sensors=[dict(s) for s in self.sensors])


class StreamTests(TestCase):
    def setUp(self):
        self.device = Device()
        for target, value in [("espargos.iq_stream._rpc", self.device.rpc),
                              ("espargos.iq_stream.time.monotonic", lambda: self.device.now),
                              ("espargos.iq_stream.time.sleep", lambda _: None)]:
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_lost_broadcast_chunk_retries_without_duplicate_samples(self):
        d = self.device
        with IQStream(d.tx) as stream:
            d.drop = (1, 1024)
            data = np.arange(8192, dtype=np.int16).astype(np.int8).tobytes()
            self.assertEqual(stream.write(data), 4096)
            self.assertEqual(d.received[0], data)
            self.assertEqual(d.received[1], data)
            stream.start()
            stream.finish()
            stream.wait()
        self.assertTrue(all(s["state"] == "closed" for s in d.sensors))

    def test_backpressure_across_fifo_capacity(self):
        d = self.device
        with IQStream(d.tx) as stream:
            stream.write(bytes(8192))
            stream.start()
            stream.write(bytes(100000))
            self.assertEqual(stream.offset, 54096)
            self.assertEqual(len(d.received[0]), 108192)

    def test_partial_write_timeout_closes_session(self):
        d = self.device
        stream = IQStream(d.tx)
        d.stuck = True
        with self.assertRaises(TimeoutError):
            stream.write(bytes(4096), timeout=.1)
        self.assertTrue(stream.closed)
        with self.assertRaises(RuntimeError):
            stream.write(bytes(4096))

    def test_reboot_during_session_is_detected(self):
        d = self.device
        stream = IQStream(d.tx)
        d.sensors[0]["boot_id"] += 1
        with self.assertRaisesRegex(RuntimeError, "restarted"):
            stream.write(bytes(4096))
        self.assertTrue(stream.closed)

    def test_encoding_and_prefill_validation(self):
        with IQStream(self.device.tx) as stream:
            self.assertEqual(stream._encode(np.array([1-1j])), bytes([127, 129]))
            for data in (b"a", np.array([complex(float("nan"), 0)]), np.array([2j])):
                with self.assertRaises(ValueError):
                    stream.write(data)
            with self.assertRaises(ValueError):
                stream.write(bytes(2 * (IQStream.CAPACITY + 1)))
            with self.assertRaises(ValueError):
                stream.start()
            with self.assertRaises(ValueError):
                stream.write(bytes(10), timeout=float("nan"))

    def test_psk31_standard_codepoints_and_chunk_boundaries(self):
        expected = "101001100101110010100"  # t / e / s / t, each followed by 00.
        self.assertEqual(psk31_bits("test", preamble=0, postamble=0), bytes(map(int, expected)))
        self.assertEqual(psk31_bits("te", postamble=0) + psk31_bits("st", preamble=0), psk31_bits("test"))
        with self.assertRaises(UnicodeEncodeError):
            psk31_bits("é")
        with IQStream(self.device.tx, mode="bpsk31") as stream:
            stream.write(psk31_bits("test"))
            with self.assertRaises(ValueError):
                stream.write(bytes([2]))


class SSBTests(TestCase):
    def test_chunk_boundaries_and_sideband_rejection(self):
        from espargos.stream_sources import SSBEncoder
        for rate in (8000, 16000):
            n = np.arange(rate)
            audio = .8*np.cos(2*np.pi*1000*n/rate)
            whole = SSBEncoder().encode(audio)
            encoder = SSBEncoder()
            pieces = np.concatenate([encoder.encode(a) for a in np.array_split(audio, 37)])
            np.testing.assert_allclose(pieces, whole, atol=1e-15)
            upper = whole[512:]
            phase = np.exp(2j*np.pi*1000*n[512:]/rate)
            wanted = abs(np.vdot(phase, upper))
            image = abs(np.vdot(phase.conj(), upper))
            self.assertLess(image / wanted, .001)
            lower = SSBEncoder(sideband="lower").encode(audio)
            np.testing.assert_allclose(lower, whole.conj())
            self.assertEqual(len(encoder.flush()), 256)
