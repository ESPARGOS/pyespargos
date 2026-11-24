import io
import struct
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from espargos.iq_transmitter import IQTransmitter
from espargos.stream_sources import SingleCarrier, GFSK
from espargos.tx_stream import transmit, transmit_packets


class StreamConfigTests(TestCase):
    def test_table_preserves_exact_wire_coefficients(self):
        original = SingleCarrier.psk31()
        loaded = SingleCarrier.from_payload(original.payload)
        self.assertEqual(loaded.payload, original.payload)
        self.assertEqual(loaded.symbol_rate_hz, 31.25)
        for data in (b"", original.payload[:-1], struct.pack("<I", 0)+original.payload[4:],
                     original.payload[:10]+struct.pack("<H", 9)+original.payload[12:]):
            with self.assertRaises(ValueError):
                SingleCarrier.from_payload(data)

    def configured(self):
        board = SimpleNamespace(host="test")
        beam = dict(ready=True, antennas=[0, 3], real=[.1]*8, imag=[.05]*8,
                    boot_id=[10]*8, crc32=[33]*8, samples=[1024]*8)
        profile = dict(mode="single_carrier", sample_rate_hz=0,
                       table_hex=SingleCarrier.psk31().payload.hex(), beam=beam)
        tx = SimpleNamespace(owner=SimpleNamespace(_lock=threading.RLock()), boards=[board],
                             states=[dict(sensors=[dict(boot_id=10, crc32=33, samples=1024) for _ in range(8)])],
                             read=lambda: None, _open_writer=lambda **settings: settings)
        return tx, profile

    def test_saved_beam_weights_and_targets_are_used(self):
        tx, profile = self.configured()
        with patch("espargos.iq_transmitter._rpc", return_value=profile):
            result = IQTransmitter.open_configured_stream(tx)
        self.assertEqual([a.antid for a in result["antennas"]], [0, 3])
        self.assertEqual(result["amplitude"], 1)
        self.assertEqual(result["modulation"].payload.hex(), profile["table_hex"])
        for value in result["steering"].values.values():
            self.assertEqual(value, .1+.05j)

    def test_only_boards_with_a_stream_source_participate(self):
        tx, profile = self.configured()
        tx.boards.insert(0, SimpleNamespace(host="reference"))
        tx.states.insert(0, {})
        with patch("espargos.iq_transmitter._rpc", side_effect=[{"beam": {"antennas": []}}, profile]):
            result = IQTransmitter.open_configured_stream(tx)
        self.assertEqual([(a.host, a.antid) for a in result["antennas"]], [("test", 0), ("test", 3)])

    def test_saved_gfsk_configuration(self):
        tx, profile = self.configured()
        modulation = GFSK.le1m()
        profile.update(mode="gfsk", table_hex=modulation.payload.hex(), ui={"gfsk_gap": 10000})
        with patch("espargos.iq_transmitter._rpc", return_value=profile):
            result = IQTransmitter.open_configured_stream(tx)
        self.assertEqual(result["modulation"].payload, modulation.payload)
        self.assertEqual(result["packet_gap_us"], 10000)

    def test_gfsk_cli_packet_boundaries_and_truncation(self):
        packets = []
        class Stream:
            started = False
            offset = 0
            capacity = 262144
            packet_gap_us = 20000
            previous_missing = 0
            def write_packet(self, data, **kwargs):
                packets.append(data)
                self.offset += len(data)*8+20000
            def start(self): self.started = True
            def finish(self): pass
            def wait(self, **kwargs): pass
            def status(self): return [dict(missing=0)]
        tx = SimpleNamespace(mute=lambda value: None)
        data = b"\x03\x00abc\x02\x00de"
        transmit_packets(io.BytesIO(data), tx, Stream())
        self.assertEqual(packets, [b"abc", b"de"])
        for broken in (b"\x03", b"\x03\x00ab", b"\xfd\x03", b""):
            with self.assertRaises(ValueError):
                transmit_packets(io.BytesIO(broken), tx, Stream())

    def test_stale_scene_and_missing_binding_are_rejected(self):
        tx, profile = self.configured()
        with patch("espargos.iq_transmitter._rpc", return_value=profile):
            tx.states[0]["sensors"][3]["crc32"] = 34
            with self.assertRaisesRegex(ValueError, "changed"):
                IQTransmitter.open_configured_stream(tx)
            profile["beam"]["ready"] = False
            with self.assertRaisesRegex(ValueError, "Stream beam"):
                IQTransmitter.open_configured_stream(tx)

    def test_cli_reads_slow_symbols_in_short_chunks(self):
        events = []
        class Stream:
            mode="single_carrier"
            stride=1
            sample_rate_hz=31.25
            capacity=65536
            chunk=4096
            previous_missing=0
            resumptions=0
            offset=0
            def __enter__(self): return self
            def __exit__(self,*args): events.append("close")
            def write(self,data): self.offset+=len(data);events.append(("write",len(data)))
            def start(self): events.append("start")
            def finish(self): events.append("finish")
            def wait(self,**kw): events.append("wait")
            def status(self): return [dict(missing=0,clipped=0)]
        stream=Stream()
        tx=SimpleNamespace(open_configured_stream=lambda:stream,mute=lambda value:events.append(("mute",value)))
        class LiveSource(io.BytesIO):
            def read1(self, size):
                assert size == 4096
                return self.read(1)
        transmit(LiveSource(bytes(20)),SimpleNamespace(tx=tx))
        self.assertEqual(events[:3],[("write",4),("mute",False),"start"])
        self.assertIn(("write",1),events)
        self.assertEqual(events[-3:],["finish","wait","close"])
        self.assertEqual(stream.offset,20)
        events.clear();stream.offset=0;stream.sample_rate_hz=1000
        transmit(io.BytesIO(bytes(20000)),SimpleNamespace(tx=tx))
        self.assertLess(len([e for e in events if isinstance(e,tuple) and e[0]=="write"]),8)
        self.assertEqual(stream.offset,20000)

    def test_cli_keeps_split_iq_pairs_and_rejects_an_odd_tail(self):
        writes=[]
        class Stream:
            mode="cs8"
            stride=2
            sample_rate_hz=8000
            capacity=65536
            chunk=4096
            previous_missing=0
            resumptions=0
            offset=0
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def write(self,data):
                assert len(data)%2==0
                writes.append(data);self.offset+=len(data)//2
            def start(self):pass
            def finish(self):pass
            def wait(self,**kw):pass
            def status(self):return [dict(missing=0,clipped=0)]
        class SplitSource(io.BytesIO):
            def read1(self,size):return self.read(1)
        stream=Stream()
        tx=SimpleNamespace(open_configured_stream=lambda:stream,mute=lambda value:None)
        data=bytes(range(256))*8+b"abcd"
        transmit(SplitSource(data),SimpleNamespace(tx=tx))
        self.assertEqual(b"".join(writes),data)
        with self.assertRaisesRegex(ValueError,"incomplete I/Q"):
            transmit(SplitSource(data[:-1]),SimpleNamespace(tx=tx))

    def test_cli_uses_one_pool_for_multiple_controllers(self):
        from espargos.tx_stream import main
        pool = SimpleNamespace(open=lambda **kw: None, close=lambda: None)
        with patch("espargos.tx_stream.Pool", return_value=pool) as factory, \
             patch("espargos.tx_stream.transmit") as send, \
             patch("espargos.tx_stream.sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"data"))):
            self.assertEqual(main(["array-a", "array-b", "--transport", "udp"]), 0)
        factory.assert_called_once_with(["array-a", "array-b"])
        self.assertIs(send.call_args.args[1], pool)

    def test_saved_profiles_preserve_per_array_weights_and_epochs(self):
        import copy
        tx, first = self.configured()
        tx.boards.append(SimpleNamespace(host="other-array"))
        second = copy.deepcopy(first)
        second["beam"].update(boot_id=[20]*8, crc32=[44]*8,
                              real=[-.2]*8, imag=[.1]*8)
        tx.states.append(dict(sensors=[dict(boot_id=20, crc32=44, samples=1024) for _ in range(8)]))
        profiles = {b.host: p for b, p in zip(tx.boards, (first, second))}
        with patch("espargos.iq_transmitter._rpc", side_effect=lambda b, _: profiles[b.host]):
            result = IQTransmitter.open_configured_stream(tx)
        values = result["steering"].values
        from espargos.calibration import Antenna
        self.assertEqual(values[Antenna("test",0)], .1+.05j)
        self.assertEqual(values[Antenna("other-array",0)], -.2+.1j)
        second["table_hex"] = SingleCarrier([-.5,.5], [.25]*33, symbol_rate_hz=1000).payload.hex()
        with patch("espargos.iq_transmitter._rpc", side_effect=lambda b, _: profiles[b.host]):
            with self.assertRaisesRegex(ValueError, "matching stream modulation"):
                IQTransmitter.open_configured_stream(tx)
