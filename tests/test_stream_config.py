import io
import struct
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from espargos.iq_transmitter import IQTransmitter
from espargos.stream_sources import SingleCarrier
from espargos.tx_stream import transmit


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
                    delay_samples=[2]*8, boot_id=[10]*8, crc32=[33]*8, samples=[1024]*8)
        profile = dict(mode="single_carrier", sample_rate_hz=0,
                       table_hex=SingleCarrier.psk31().payload.hex(), beam=beam)
        tx = SimpleNamespace(owner=SimpleNamespace(_lock=threading.RLock()), boards=[board],
                             states=[dict(sensors=[dict(boot_id=10, crc32=33, samples=1024) for _ in range(8)])],
                             read=lambda: None, open_stream=lambda **settings: settings)
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
        for delay in result["steering"].delays_s.values():
            self.assertEqual(delay, 2/16000000)

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
