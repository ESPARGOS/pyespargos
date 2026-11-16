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
    fields = (
        "session",
        "state",
        "error",
        "accepted",
        "consumed",
        "end",
        "format",
        "input_rate_hz",
        "capacity",
        "rejected",
        "rendered_blocks",
        "max_render_us",
        "max_late_us",
        "clipped",
        "sequence",
        "max_source_us",
        "max_expand_us",
        "missing",
        "late",
        "table_received",
    )

    def __init__(self):
        self.now = 0.0
        self.sequence = 0
        self.callback = None
        self.drop = None
        self.stuck = False
        self.muted = False
        self.received = [bytearray(), bytearray()]
        self.frames = []
        self.sensors = [dict(available=True, boot_id=10 + a, session=0, state="closed", error=0, accepted=0, consumed=0, capacity=IQStream.CAPACITY, end=None, missing=0, late=0, table_received=0) for a in range(2)]
        self.board = SimpleNamespace(host="test", control=SimpleNamespace(fetch=self.send), send_stream_data=lambda data: self.send("data", data), subscribe_controller_messages=self.subscribe, unsubscribe_controller_messages=lambda sub: None)

        # IQStream keys feedback by Board identity, as real Board objects do.
        class Board:
            pass

        board = Board()
        board.__dict__.update(self.board.__dict__)
        self.board = board
        sensors = [dict(applied_mode="iq_tx", applied_sample_rate_hz=16000000, samples=1024, boot_id=10 + a, frequency_hz=2437000000, enabled=True, sync_seq=7, sample_position=0, epoch=1) for a in range(8)]
        self.tx_state = dict(sensors=sensors, modes=["iq_tx"] * 8, muted=False, sample_rate_hz=16000000)
        self.tx = SimpleNamespace(boards=[self.board], states=[self.tx_state], read=lambda: None, signals=SimpleNamespace(_antennas=lambda _: [Antenna("test", a) for a in range(2)], _check_beam_calibration=lambda: None))

    def subscribe(self, magic, callback):
        assert magic == 0x32535449
        self.callback = callback
        self.publish()
        return callback

    def publish(self):
        if not self.callback:
            return
        self.sequence += 1
        words = [0x32535449, 2, self.sequence, 0, 0, 0]
        for a in range(8):
            state = self.sensors[a] if a < 2 else dict(boot_id=10 + a, state="closed")
            words += [state["boot_id"], int(a < 2)]
            for key in self.fields:
                value = state.get(key, 0)
                if key == "state":
                    value = ("closed", "buffering", "armed", "running", "drained", "fault").index(value)
                if key == "end" and value is None:
                    value = 0xFFFFFFFF
                words.append(value)
        self.callback(struct.pack("<182I", *words))

    def sleep(self, seconds):
        self.now += max(seconds, 0.001)
        for s in self.sensors:
            if s["state"] == "running" and not self.stuck:
                s["consumed"] = min(s["accepted"], s["consumed"] + 1024)
                if s["end"] is not None and s["consumed"] == s["end"]:
                    s["state"] = "drained"
        self.publish()

    def send(self, path, data):
        magic, version, command, mask, reserved, session, offset, count = struct.unpack_from("<IBBBBIII", data)
        assert magic == 0x31535449 and version == 2 and not reserved
        payload = data[20:]
        self.frames.append((path, command, count))
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
                if offset >= s["accepted"]:
                    assert offset + count - s["consumed"] <= IQStream.CAPACITY
                    gap = offset - s["accepted"]
                    self.received[a].extend(bytes(gap * self.stride))
                    self.received[a].extend(payload)
                    s["missing"] += gap
                    s["accepted"] = offset + count
                else:
                    s["late"] += count
            elif command == 3:
                s["state"] = "running"
            elif command == 4:
                gap = offset - s["accepted"]
                self.received[a].extend(bytes(gap * self.stride))
                s["missing"] += gap
                s["accepted"] = offset
                s["end"] = offset
            elif command == 5:
                s["state"] = "closed"
            elif command == 6:
                s["table_received"] = offset + count
        self.publish()
        return b"sent"

    def rpc(self, board, path, data=None):
        if path == "get_iq_tx":
            return self.tx_state
        if path == "set_iq_tx":
            self.muted = data["muted"]
            return {}
        raise AssertionError("No HTTP status polling: " + path)


class StreamTests(TestCase):
    def setUp(self):
        self.device = Device()
        for target, value in [("espargos.iq_stream._rpc", self.device.rpc), ("espargos.iq_stream.time.monotonic", lambda: self.device.now), ("espargos.iq_stream.time.sleep", self.device.sleep)]:
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_udp_loss_preserves_offsets_without_retransmission(self):
        d = self.device
        with IQStream(d.tx) as stream:
            d.drop = (1, 512)
            data = bytes([17]) * 8192
            stream.write(data)
            self.assertEqual(d.received[0], data)
            self.assertEqual(d.received[1], data[:1024] + bytes(1024) + data[2048:])
            self.assertEqual(stream.status()[1]["missing"], 512)
            stream.start()
            stream.finish()
            stream.wait()

    def test_missing_final_datagram_is_filled_by_finish(self):
        d = self.device
        with IQStream(d.tx) as stream:
            d.drop = (1, 1536)
            stream.write(bytes([17]) * 4096)
            stream.finish()
            self.assertEqual(d.sensors[1]["missing"], 512)
            self.assertEqual(d.sensors[1]["accepted"], 2048)

    def test_backpressure_across_fifo_capacity(self):
        d = self.device
        with IQStream(d.tx) as stream:
            stream.write(bytes(8192))
            stream.start()
            stream.write(bytes(100000))
            self.assertEqual(stream.offset, 54096)

    def test_stalled_consumption_times_out_and_closes(self):
        d = self.device
        stream = IQStream(d.tx)
        stream.write(bytes(8192))
        stream.start()
        d.stuck = True
        with self.assertRaises(TimeoutError):
            stream.write(bytes(100000), timeout=0.5)
        self.assertTrue(stream.closed)

    def test_generic_symbols_and_pulse_table(self):
        from espargos.stream_sources import SingleCarrier, psk31_symbols

        d = self.device
        modulation = SingleCarrier.psk31()
        with IQStream(d.tx, mode="single_carrier", modulation=modulation) as stream:
            data = psk31_symbols("test")
            stream.write(data)
            self.assertEqual(d.received[0], data)
            self.assertEqual(d.sensors[0]["table_received"], len(modulation.payload))
            with self.assertRaises(ValueError):
                stream.write(bytes([3]))
        with self.assertRaises(ValueError):
            SingleCarrier([1, -1], [1] * 33, symbol_rate_hz=1000)

    def test_fast_pulse_limits_and_ordered_finish(self):
        from espargos.stream_sources import SingleCarrier

        mod = SingleCarrier([.5, 0], [1] * 33, symbol_rate_hz=100000)
        with IQStream(self.device.tx, mode="single_carrier", modulation=mod) as stream:
            stream.write(bytes(2000))
            stream.finish()
        frames = [f for f in self.device.frames if f[1] in (2, 4)]
        self.assertEqual(frames, [("data", 2, 1024), ("data", 2, 976), ("data", 4, 0)])
        for points, pulse, rate in [([0]*65, [1]*33, 1000), ([0], [1]*289, 1000),
                                     ([0], [1]*65, 50000), ([.5, 0], [1]*33, 99900)]:
            with self.subTest(points=len(points), pulse=len(pulse), rate=rate), self.assertRaises(ValueError):
                SingleCarrier(points, pulse, symbol_rate_hz=rate)

    def test_stale_feedback_stops_uploads(self):
        d = self.device
        stream = IQStream(d.tx)
        d.now += 1
        # No new reports while the clock advances through condition waits.
        with patch.object(stream._feedback_changed, "wait", side_effect=lambda seconds: setattr(d, "now", d.now + seconds)):
            with self.assertRaisesRegex(TimeoutError, "feedback is stale"):
                stream._states()
        d.publish()
        stream.close()

    def test_old_feedback_does_not_restore_credit_or_freshness(self):
        d = self.device
        with IQStream(d.tx) as stream:
            stream.write(bytes(4096))
            old = stream._feedback[d.board]
            d.sequence -= 2
            d.sensors[0]["consumed"] = 9999
            d.publish()
            self.assertEqual(stream._feedback[d.board], old)
            d.sensors[0]["consumed"] = 0
            d.sequence += 3
            d.publish()
            self.assertEqual(stream.transport_status()["test"]["rejected"], 0)

    def test_closed_status_is_last_received_snapshot(self):
        d = self.device
        stream = IQStream(d.tx)
        stream.close()
        d.now += 10
        self.assertTrue(all(s["state"] == "closed" for s in stream.status()))

    def test_reboot_is_detected(self):
        d = self.device
        stream = IQStream(d.tx)
        d.sensors[0]["boot_id"] += 1
        d.publish()
        with self.assertRaisesRegex(RuntimeError, "restarted"):
            stream.write(bytes(2048))
        self.assertTrue(stream.closed)


class SSBTests(TestCase):
    def test_chunk_boundaries_and_sideband_rejection(self):
        from espargos.stream_sources import SSBEncoder

        for rate in (8000, 16000, 32000):
            n = np.arange(rate)
            audio = 0.8 * np.cos(2 * np.pi * 1000 * n / rate)
            whole = SSBEncoder().encode(audio)
            encoder = SSBEncoder()
            pieces = np.concatenate([encoder.encode(a) for a in np.array_split(audio, 37)])
            np.testing.assert_allclose(pieces, whole, atol=1e-15)
            upper = whole[512:]
            phase = np.exp(2j * np.pi * 1000 * n[512:] / rate)
            wanted = abs(np.vdot(phase, upper))
            image = abs(np.vdot(phase.conj(), upper))
            self.assertLess(image / wanted, 0.001)
            lower = SSBEncoder(sideband="lower").encode(audio)
            np.testing.assert_allclose(lower, whole.conj())
            self.assertEqual(len(encoder.flush()), 256)
