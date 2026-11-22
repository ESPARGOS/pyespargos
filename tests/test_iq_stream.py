"""Exercise stream transport acknowledgements, credit and failure recovery."""

import struct
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import numpy as np
from espargos.calibration import Antenna
from espargos.iq_stream import IQStream
from espargos.stream_sources import psk31_bits, GFSK, ble_le1m_packet


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
        self.wire_frames = []
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
        words = [0x32535449, 4, self.sequence, 0, 0, 0]
        for a in range(8):
            state = self.sensors[a] if a < 2 else dict(boot_id=10 + a, state="closed")
            words += [state["boot_id"], int(a < 2)]
            for key in self.fields:
                value = state.get(key, 0)
                if key == "state":
                    value = ("closed", "buffering", "armed", "running", "drained", "fault", "starved").index(value)
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
        assert magic == 0x31535449 and version == 4 and not reserved
        payload = data[20:]
        self.frames.append((path, command, count))
        self.wire_frames.append(bytes(data))
        for a, s in enumerate(self.sensors):
            if not mask & (1 << a):
                continue
            if command == 1:
                s.update(session=session, state="buffering", accepted=0, consumed=0, end=None, missing=0, late=0, error=0, table_received=0)
                self.bits = 8
                self.format=struct.unpack_from("<I",payload)[0]
                self.stride = 1 if self.format==2 else 2
                s["capacity"] = IQStream.CAPACITY * {1:1,2:8,3:8}[self.format]
            elif command == 2:
                decoded = payload
                if self.stride == 1:
                    decoded = bytes(sum(((payload[(k*self.bits+j)//8] >> ((k*self.bits+j)%8)) & 1) << j for j in range(self.bits)) for k in range(count))
                if self.drop == (a, offset):
                    self.drop = None
                    continue
                if offset >= s["accepted"]:
                    assert offset + count - s["consumed"] <= s["capacity"]
                    gap = offset - s["accepted"]
                    self.received[a].extend(bytes(gap * self.stride))
                    self.received[a].extend(decoded)
                    s["missing"] += gap
                    s["accepted"] = offset + count
                else:
                    s["late"] += count
            elif command == 7:
                bits,reserved=struct.unpack_from("<HH",payload)
                assert self.format==3 and not reserved and len(payload)==4+(bits+7)//8
                if self.drop==(a,offset):self.drop=None;continue
                if offset>=s["accepted"]:
                    gap=offset-s["accepted"];s["missing"]+=gap
                    self.received[a].extend(payload[4:]);s["accepted"]=offset+count
                else:s["late"]+=count
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
                if offset == 0:
                    self.bits = max(1, (struct.unpack_from("<H", payload, 8)[0] - 1).bit_length())
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

    def test_start_waits_for_the_entire_prefill(self):
        with IQStream(self.device.tx) as stream:
            stream.write(bytes(8192))
            self.device.sensors[1]["accepted"] = 1024
            self.device.publish()
            original_send = self.device.board.control.fetch
            def send(path, data):
                if data[5] == 3:
                    self.assertEqual(self.device.sensors[1]["accepted"], 4096)
                return original_send(path, data)
            def deliver(seconds):
                self.device.sensors[1]["accepted"] = 4096
                self.device.sleep(seconds)
            self.device.board.control.fetch = send
            with patch("espargos.iq_stream.time.sleep", deliver):
                stream.start()

    def test_gfsk_atomic_packets_and_loss(self):
        air=ble_le1m_packet(bytes.fromhex("0206475250534502"))
        with IQStream(self.device.tx,mode="gfsk",modulation=GFSK.le1m()) as stream:
            self.assertEqual(stream.capacity,524288)
            self.device.drop=(1,0)
            self.assertEqual(stream.write_packet(air,gap_us=1000),len(air)*8)
            units=len(air)*8+1000
            stream.write_packet(air,gap_us=1000)
            self.assertEqual(self.device.sensors[1]["missing"],units)
            self.assertEqual(self.device.received[1],air)
            self.assertEqual(stream.offset,2*units)
            self.assertTrue(all(path=="data" for path,command,_ in self.device.frames if command==7))
            with self.assertRaises(ValueError):stream.write(b"not a packet")
            with self.assertRaises(ValueError):stream.write_packet(bytes(1021))
            with self.assertRaises(ValueError):stream.write_packet(air,gap_us=70000)
            stream.finish()

    def test_ble_air_bytes_decode_independently(self):
        from espargos.ble_decoder import decode
        pdu=bytes.fromhex("0206475250534502")
        for channel in [37,38,39]:
            air=ble_le1m_packet(pdu,channel=channel)
            bits=np.unpackbits(np.frombuffer(air,np.uint8),bitorder="little")*2.-1
            t=np.arange(-16,17)/4
            taps=np.exp(-2*(np.pi*.5*t)**2/np.log(2));taps/=taps.sum()
            levels=np.pad(np.repeat(bits,4),(16,16),mode="edge")
            f=np.convolve(levels,taps,"same")[16:-16]
            phase=-np.cumsum(f)*2*np.pi*.25/4+np.pi*np.arange(len(f))
            z=np.pad(150*np.exp(1j*phase),(160,64))
            raw=((np.rint(z.real).astype(np.int32)&1023)|((np.rint(z.imag).astype(np.int32)&1023)<<10)).astype(np.uint32)
            packet=decode(raw,8,channel,0)
            self.assertIsNotNone(packet)
            self.assertEqual(packet.flags,3)
            self.assertEqual(packet.prefix,pdu)

    def test_le2m_air_bytes_decode_independently(self):
        from espargos.ble_decoder import decode
        from espargos.stream_sources import ble_le2m_packet

        pdu = bytes.fromhex("07080701475250534502")  # AUX_ADV_IND with AdvA.
        for channel in [0, 12, 36]:
            air = ble_le2m_packet(pdu, channel=channel)
            one_m = ble_le1m_packet(pdu, channel=channel)
            self.assertEqual(air, one_m[:1] + one_m)
            bits = np.unpackbits(np.frombuffer(air, np.uint8), bitorder="little") * 2. - 1
            t = np.arange(-16, 17) / 4
            taps = np.exp(-2 * (np.pi * .5 * t) ** 2 / np.log(2))
            taps /= taps.sum()
            levels = np.pad(np.repeat(bits, 4), (16, 16), mode="edge")
            frequency = np.convolve(levels, taps, "same")[16:-16]
            phase = -np.cumsum(frequency) * 2 * np.pi * .25 / 4 + np.pi / 2 * np.arange(len(frequency))
            z = np.pad(150 * np.exp(1j * phase), (160, 64))
            raw = ((np.rint(z.real).astype(np.int32) & 1023) |
                   ((np.rint(z.imag).astype(np.int32) & 1023) << 10)).astype(np.uint32)
            packet = decode(raw, 4, channel, 1)
            self.assertIsNotNone(packet)
            self.assertEqual(packet.flags, 3)
            self.assertEqual(packet.prefix, pdu)
        self.assertEqual(GFSK.le2m().symbol_rate_hz, 2_000_000)
        with self.assertRaises(ValueError):
            ble_le2m_packet(pdu, channel=37)
        for span, index in [(4, .25), (2, 1 / 16)]:
            with self.assertRaises(ValueError):
                GFSK(symbol_rate_hz=2_000_000, span=span, modulation_index=index)

    def test_gfsk_configuration_roundtrip(self):
        for rate in [125000,250000,500000,1000000,16000000/12,1600000,2000000]:
            g=GFSK(symbol_rate_hz=rate,span=2,modulation_index=1)
            self.assertEqual(GFSK.from_payload(g.payload).payload,g.payload)
        for params in [dict(symbol_rate_hz=500000),dict(bt=float("nan")),dict(span=3),dict(symbol_rate_hz=900000),dict(modulation_index=.51),dict(symbol_rate_hz=125000,modulation_index=1/16)]:
            with self.assertRaises(ValueError):GFSK(**params)

    def test_high_rate_single_carrier_roundtrip(self):
        from espargos.stream_sources import SingleCarrier

        for rate in [1000000, 16000000 / 12, 1600000]:
            for span in [1, 2, 4]:
                t = np.arange(span * 32 + 1) / 32 - span / 2
                pulse = .8 * np.exp(-.5 * (t / .4) ** 2)
                modulation = SingleCarrier([-1, 1], pulse, symbol_rate_hz=rate)
                self.assertEqual(SingleCarrier.from_payload(modulation.payload).payload,
                                 modulation.payload)
        with self.assertRaises(ValueError):
            SingleCarrier([-1, 1], np.ones(33), symbol_rate_hz=1600001)
        with self.assertRaises(ValueError):
            GFSK(symbol_rate_hz=2000001)

    def test_cs8_uses_raw_signed_byte_pairs(self):
        with IQStream(self.device.tx, mode="cs8") as stream:
            self.assertEqual(stream.stride, 2)
            frame = next(f for f in reversed(self.device.wire_frames) if f[5] == 1)
            self.assertEqual(struct.unpack_from("<I", frame, 20)[0], 1)
            stream.write(bytes([1, 255]) * 16)
            self.assertEqual(self.device.received[0], bytes([1, 255]) * 16)

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
            stream.write(bytes(200000), timeout=0.5)
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

    def test_fast_pulse_limits_and_ordered_finish(self):
        from espargos.stream_sources import SingleCarrier

        mod = SingleCarrier([.5, 0], [1] * 33, symbol_rate_hz=100000)
        with IQStream(self.device.tx, mode="single_carrier", modulation=mod) as stream:
            stream.write(bytes(2000))
            stream.finish()
        frames = [f for f in self.device.frames if f[1] in (2, 4)]
        self.assertEqual(frames, [("data", 2, 2000), ("data", 4, 0)])
        two_symbols = SingleCarrier([-.5, .5], [.3] * 65, symbol_rate_hz=32000)
        self.assertEqual(SingleCarrier.from_payload(two_symbols.payload).symbol_rate_hz, 32000)
        for points, pulse, rate in [([0]*66, [1]*33, 1000), ([0], [1]*289, 1000),
                                     ([0], [1]*65, 50000), ([.5, 0], [1]*33, 99900),
                                     ([0], [.3]*65, 32001), ([0], [.1]*129, 16001)]:
            with self.subTest(points=len(points), pulse=len(pulse), rate=rate), self.assertRaises(ValueError):
                SingleCarrier(points, pulse, symbol_rate_hz=rate)

    def test_64qam_uses_all_six_bit_indices(self):
        from espargos.stream_sources import SingleCarrier

        points = [complex(i, q) / 98**.5 for i in range(-7, 8, 2) for q in range(-7, 8, 2)]
        modulation = SingleCarrier(points, [.1] * 129, symbol_rate_hz=1000)
        with IQStream(self.device.tx, mode="single_carrier", modulation=modulation) as stream:
            data = bytes(range(64))
            stream.write(data)
            self.assertEqual(self.device.received[0], data)
            with self.assertRaises(ValueError):
                stream.write(bytes([64]))
        largest = SingleCarrier(points, [0] * 1025, symbol_rate_hz=1000, samples_per_symbol=128)
        self.assertEqual(len(largest.payload), 4368)
        self.assertEqual(SingleCarrier.from_payload(largest.payload).payload, largest.payload)
        with self.assertRaises(ValueError):
            SingleCarrier(points + [0], [.1] * 129, symbol_rate_hz=1000)

    def test_packed_packets_restart_bit_alignment(self):
        from espargos.stream_sources import SingleCarrier

        mod = SingleCarrier([-0.5, 0.5], [1] * 33, symbol_rate_hz=250000)
        with IQStream(self.device.tx, mode="single_carrier", modulation=mod) as stream:
            stream.write(bytes([1, 0, 1]))
            stream.write(bytes([0, 1, 1, 0, 1]))
            data = [f for f in self.device.wire_frames if f[5] == 2]
            self.assertEqual([f[20:] for f in data], [b"\x05", b"\x16"])
            self.assertEqual([struct.unpack_from("<II", f, 12) for f in data], [(0, 3), (3, 5)])
            self.assertEqual(self.device.received[0], bytes([1,0,1,0,1,1,0,1]))

    def test_underrun_recovers_every_sensor_on_original_symbol_grid(self):
        from espargos.stream_sources import SingleCarrier

        d = self.device
        mod = SingleCarrier([-.5, .5], [.25] * 65, symbol_rate_hz=1234.5)
        with IQStream(d.tx, mode="single_carrier", modulation=mod) as stream:
            stream.write(bytes(64));stream.start()
            old_session, old_origin = stream.session, stream.start_sample * 256
            original_lock = stream._lock
            d.sensors[0].update(state="starved", missing=1234)
            d.publish()
            for sensor in d.tx_state["sensors"]:
                sensor["sample_position"] += 13000000
            stream.write(bytes([1]) * 256)
            self.assertNotEqual(stream.session, old_session)
            self.assertIs(stream._lock, original_lock)
            self.assertEqual(stream.resumptions, 1)
            self.assertEqual(stream.previous_missing, 1234)
            period = struct.unpack_from("<I", mod.payload)[0]
            self.assertEqual((stream.start_sample * 256 - stream.start_phase_q8 - old_origin) % period, 0)
            self.assertEqual([s["state"] for s in d.sensors], ["running", "running"])
            self.assertEqual(d.received[0][-256:], bytes([1]) * 256)
            self.assertEqual(d.received[0], d.received[1])
            arms = [f for f in d.wire_frames if f[5] == 3]
            self.assertEqual(len(arms), 2)
            self.assertEqual(struct.unpack_from("<I", arms[-1], 64)[0], stream.start_phase_q8)
            stream.finish();stream.wait()

    def test_underrun_mid_write_resumes_from_unsent_samples(self):
        device = self.device
        prefill = bytes(2048)
        payload = np.arange(12000, dtype="<u2").tobytes()
        with IQStream(device.tx) as stream:
            stream.write(prefill)
            stream.start()
            original_session = stream.session
            stock_send = device.board.send_stream_data

            def send_then_starve(frame):
                stock_send(frame)
                _, _, command, _, _, session, offset, count = struct.unpack_from("<IBBBBIII", frame)
                if command == 2 and session == original_session and offset + count == 5120:
                    device.sensors[0].update(state="starved", missing=12)
                    device.publish()

            device.board.send_stream_data = send_then_starve
            self.assertEqual(stream.write(payload), 12000)
            self.assertEqual(stream.resumptions, 1)
            self.assertEqual(stream.offset, 12000 - 4096)
            self.assertEqual(device.received[0], prefill + payload)
            self.assertEqual(device.received[1], prefill + payload)
            stream.finish()
            stream.wait()

    def test_bulk_writes_wait_for_full_packet_credit(self):
        d = self.device
        for sensor in d.sensors:
            sensor["capacity"] = 5000
        d.publish()
        with IQStream(d.tx) as stream:
            stream.write(bytes(4096));stream.start()
            stream.write(bytes(60000))
            frames = [count for _, command, count in d.frames if command == 2]
            self.assertTrue(all(count == 512 for count in frames[:-1]))
            self.assertEqual(d.received[0], bytes(64096))

    def test_packed_bpsk_fills_payload_with_more_symbols(self):
        from espargos.stream_sources import SingleCarrier

        d = self.device
        mod = SingleCarrier([-.5, .5], [1] * 33, symbol_rate_hz=250000)
        for sensor in d.sensors:
            sensor["capacity"] = 20000
        d.publish()
        with IQStream(d.tx, mode="single_carrier", modulation=mod) as stream:
            stream.write(bytes([0, 1]) * 8192)
            stream.start()
            stream.write(bytes([1, 0]) * 20000)
            packets = [f for f in d.wire_frames if f[5] == 2]
            self.assertTrue(all(len(f) == 1044 for f in packets[:-1]))
            self.assertTrue(all(struct.unpack_from("<I", f, 16)[0] == 8192 for f in packets[:-1]))
            self.assertEqual(d.received[0], bytes([0, 1]) * 8192 + bytes([1, 0]) * 20000)

    def test_mode_exit_stops_writes_without_waiting_for_fifo_credit(self):
        d = self.device
        with IQStream(d.tx) as stream:
            stream.write(bytes(4096))
            stream.start()
            for sensor in d.sensors:
                sensor.update(state="closed", consumed=0)
            d.publish()
            count = len(d.wire_frames)
            with self.assertRaisesRegex(RuntimeError, "stream closed on sensor"):
                stream.write(bytes(4096))
            self.assertEqual(len(d.wire_frames), count)

    def test_open_memory_failure_is_reported_and_closed(self):
        d = self.device
        original = d.board.control.fetch

        def reject_open(path, frame):
            result = original(path, frame)
            if frame[5] == 1:
                for sensor in d.sensors:
                    sensor.update(state="fault", error=0x101)
                d.publish()
            return result

        d.board.control.fetch = reject_open
        with self.assertRaisesRegex(RuntimeError, "insufficient sensor memory"):
            IQStream(d.tx)
        self.assertTrue(all(sensor["state"] == "closed" for sensor in d.sensors))
        self.assertFalse(d.muted)

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


class MultiArrayStreamTests(TestCase):
    """Controllers have unrelated boot/epoch IDs but one calibrated grid."""

    def setUp(self):
        import concurrent.futures
        from types import MethodType
        from espargos.iq_transmitter import IQTransmitter
        from espargos.calibration import Calibration, CalibrationGroup, CalibrationState, AntennaCorrection

        self.now = 0.0
        self.devices = [Device(), Device()]
        self.devices[0].board.host = "array-a"
        self.devices[1].board.host = "array-b"
        self.by_board = {d.board: d for d in self.devices}
        self.latencies = {d.board: latency for d, latency in zip(self.devices, (.013, .18))}
        self.antennas = [Antenna(d.board.host, a) for d in self.devices for a in range(2)]
        corrections = []
        for bi, d in enumerate(self.devices):
            for a, sensor in enumerate(d.tx_state["sensors"]):
                sensor.update(antid=a, sample_rate_hz=16000000, boot_id=100 * bi + 10 + a,
                              epoch=1000 * bi + a + 1, origin_ns=1000000 * (bi + 1) + a,
                              sample_position=1000000 + 1000 * bi)
                if a < 2:
                    d.sensors[a]["boot_id"] = sensor["boot_id"]
                    state = CalibrationState.from_tx_status(d.board.host, sensor)
                    corrections.append(AntennaCorrection(state, .1 * (bi + a), 1e-9 * a))
        self.tx = SimpleNamespace(
            boards=[d.board for d in self.devices], states=[d.tx_state for d in self.devices],
            read=lambda: None,
            signals=SimpleNamespace(_antennas=lambda _: self.antennas, _check_beam_calibration=lambda: None),
            owner=SimpleNamespace(calibrations=Calibration((CalibrationGroup(tuple(corrections)),))),
            positions=np.array([[[bi + a * .06, 0, 0] for a in range(8)] for bi in range(2)]),
            workers=concurrent.futures.ThreadPoolExecutor(max_workers=2),
        )
        self.tx._parallel = MethodType(IQTransmitter._parallel, self.tx)
        self.addCleanup(self.tx.workers.shutdown)
        for target, value in [("espargos.iq_stream._rpc", self.rpc),
                              ("espargos.iq_stream.time.monotonic", lambda: self.now),
                              ("espargos.iq_stream.time.sleep", self.sleep)]:
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def sleep(self, seconds):
        step = max(seconds, .001)
        self.now += step
        for d in self.devices:
            for sensor in d.tx_state["sensors"]:
                sensor["sample_position"] += round(step * IQStream.RATE)
            d.sleep(step)

    def rpc(self, board, path, data=None):
        import copy
        d = self.by_board[board]
        if path == "get_iq_tx":
            # A reply contains the clock at request processing, but arrives
            # later. Different network delays must not shorten the lead.
            snapshot = copy.deepcopy(d.tx_state)
            self.sleep(self.latencies[board])
            return snapshot
        return d.rpc(board, path, data)

    def arms(self, device):
        return [struct.unpack_from("<QI8II", f, 20) for f in device.wire_frames if f[5] == 3]

    def test_all_formats_share_origin_epochs_and_geometry_across_arrays(self):
        from espargos.steering import Beamspace
        from espargos.stream_sources import SingleCarrier

        modes = [dict(mode="cs8"),
                 dict(mode="single_carrier", modulation=SingleCarrier.psk31()),
                 dict(mode="gfsk", modulation=GFSK.le1m())]
        for settings in modes:
            with self.subTest(mode=settings["mode"]):
                steering = Beamspace(azimuth_deg=17)
                with IQStream(self.tx, steering=steering, **settings) as stream:
                    if stream.mode == "gfsk":
                        stream.write_packet(b"abcd", gap_us=20000)
                    else:
                        stream.write(bytes(4096))
                    stream.start(lead_time=.5)
                    arms = [self.arms(d)[-1] for d in self.devices]
                    self.assertEqual(arms[0][0:2], arms[1][0:2])
                    self.assertEqual(arms[0][-1], arms[1][-1])
                    self.assertEqual(arms[0][0], stream.start_sample)
                    self.assertEqual(stream.start_sample % 32, 0)
                    latest = max(s["sample_position"] for d in self.devices for s in d.tx_state["sensors"][:2])
                    self.assertGreaterEqual(stream.start_sample - latest, .5 * IQStream.RATE)
                    for d, arm in zip(self.devices, arms):
                        self.assertEqual(arm[2:10], tuple(s["epoch"] for s in d.tx_state["sensors"]))
                        opened = [f for f in d.wire_frames if f[5] == 1][-1]
                        config = struct.unpack_from("<4I16f", opened, 20)
                        bi = self.devices.index(d)
                        for a in range(2):
                            weight = steering.coefficients(self.tx.positions[bi, a], 2437000000, None)
                            self.assertAlmostEqual(config[4 + a], weight.real * .1, places=6)
                            self.assertAlmostEqual(config[12 + a], weight.imag * .1, places=6)
                    self.assertNotEqual(arms[0][2:10], arms[1][2:10])
                    self.assertEqual(self.devices[0].received[0], self.devices[1].received[0])

    def test_arm_requests_are_concurrent(self):
        import threading
        barrier = threading.Barrier(2)
        for d in self.devices:
            original = d.board.control.fetch
            def send(path, data, original=original):
                if data[5] == 3:
                    barrier.wait(timeout=2)
                return original(path, data)
            d.board.control.fetch = send
        with IQStream(self.tx) as stream:
            stream.write(bytes(4096))
            stream.start()
        self.assertEqual(self.arms(self.devices[0])[-1][0], self.arms(self.devices[1])[-1][0])

    def test_incompatible_grids_are_rejected_before_any_arm(self):
        self.devices[1].tx_state["sensors"][0]["sync_seq"] = 99
        with IQStream(self.tx) as stream:
            stream.write(bytes(4096))
            with self.assertRaisesRegex(ValueError, "synchronization sequence"):
                stream.start()
            self.assertFalse(any(self.arms(d) for d in self.devices))

    def test_one_array_underrun_recovers_every_array_on_the_original_grid(self):
        from espargos.stream_sources import SingleCarrier
        modulation = SingleCarrier([-.5, .5], [.25] * 65, symbol_rate_hz=1234.5)
        with IQStream(self.tx, mode="single_carrier", modulation=modulation) as stream:
            stream.write(bytes(128))
            stream.start()
            origin, session = stream._phase_origin, stream.session
            self.devices[1].sensors[0].update(state="starved", missing=77)
            self.devices[1].publish()
            stream.write(bytes([1]) * 256)
            self.assertNotEqual(stream.session, session)
            self.assertEqual(stream.resumptions, 1)
            self.assertEqual(stream.previous_missing, 77)
            arms = [self.arms(d)[-1] for d in self.devices]
            self.assertEqual(arms[0][0:2], arms[1][0:2])
            self.assertEqual(arms[0][-1], arms[1][-1])
            period = struct.unpack_from("<I", modulation.payload)[0]
            self.assertEqual((stream.start_sample * 256 - stream.start_phase_q8 - origin) % period, 0)
            self.assertTrue(all(s["state"] == "running" for d in self.devices for s in d.sensors))

    def test_independently_calibrated_arrays_cannot_form_a_geometric_beam(self):
        from espargos.calibration import Calibration, CalibrationGroup
        from espargos.steering import Beamspace
        corrections = self.tx.owner.calibrations.groups[0].corrections
        self.tx.owner.calibrations = Calibration(tuple(
            CalibrationGroup(tuple(c for c in corrections if c.state.antenna.host == d.board.host))
            for d in self.devices
        ))
        with self.assertRaisesRegex(ValueError, "independent calibration references"):
            IQStream(self.tx, steering=Beamspace())
        self.assertFalse(any(d.wire_frames for d in self.devices))

    def test_failed_arm_joins_other_arrays_before_closing_them(self):
        import threading
        barrier = threading.Barrier(2)
        for bi, d in enumerate(self.devices):
            original = d.board.control.fetch
            def send(path, data, original=original, bi=bi):
                if data[5] == 3:
                    barrier.wait(timeout=2)
                    if bi == 0:
                        raise ConnectionError("array A disconnected")
                return original(path, data)
            d.board.control.fetch = send
        with IQStream(self.tx) as stream:
            stream.write(bytes(4096))
            with self.assertRaisesRegex(ConnectionError, "disconnected"):
                stream.start()
            self.assertTrue(stream.closed)
        self.assertTrue(all(s["state"] == "closed" for d in self.devices for s in d.sensors))
        commands = [frame[5] for frame in self.devices[1].wire_frames]
        self.assertLess(commands.index(3), commands.index(5))
