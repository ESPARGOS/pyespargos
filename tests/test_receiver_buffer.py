"""Overload is bounded and observable without blocking stream producers."""

import threading
import unittest
from espargos import BufferPolicy, Pool
from espargos.receiver_buffer import ReceiverBuffer
from test_ble_api import FakeBoard


class BufferTests(unittest.TestCase):
    def test_live_overflow_retains_latest_reports(self):
        buffer = ReceiverBuffer(BufferPolicy(capacity=3, batch_size=2))
        for i in range(10):
            buffer.put_nowait(i)
        self.assertEqual(buffer.take(), [7, 8])
        self.assertEqual(buffer.take(), [9])
        self.assertEqual(buffer.stats.dropped, 7)
        self.assertEqual(buffer.stats.received, 10)
        self.assertEqual(buffer.stats.high_watermark, 3)

    def test_recording_retention_and_expiry_are_distinct(self):
        now = [1.0]
        buffer = ReceiverBuffer(BufferPolicy(capacity=2, overflow="newest"), clock=lambda: now[0])
        for i in range(4):
            buffer.put_nowait(i)
        self.assertEqual(buffer.take(), [0, 1])
        buffer.configure(BufferPolicy(capacity=None, max_age=0.5))
        buffer.put_nowait(4)
        now[0] = 2.0
        buffer.put_nowait(5)
        self.assertEqual(buffer.take(), [5])
        self.assertEqual(buffer.stats.dropped, 2)
        self.assertEqual(buffer.stats.expired, 1)

    def test_resize_counts_loss_and_close_wakes_reader(self):
        buffer = ReceiverBuffer(BufferPolicy(capacity=None))
        for i in range(20):
            buffer.put_nowait(i)
        buffer.configure(BufferPolicy(capacity=2))
        self.assertEqual(buffer.stats.dropped, 18)
        self.assertEqual(buffer.take(), [18, 19])
        done = threading.Event()
        thread = threading.Thread(target=lambda: (buffer.take(None), done.set()))
        thread.start()
        buffer.close()
        self.assertTrue(done.wait(1))
        thread.join()

    def test_receiver_overload_keeps_complete_new_events(self):
        board = FakeBoard()
        with Pool([board]) as pool:
            receiver = pool.ble
            receiver.configure_buffering(BufferPolicy(capacity=16, batch_size=8))
            seen = []
            receiver.add_callback(lambda c: seen.append(c.packet.sequence))
            for seq in range(1, 11):
                for antenna in range(8):
                    board.emit(antenna, sequence=seq, time_us=seq * 10000)
            self.assertEqual(receiver.buffer_stats.dropped, 64)
            self.assertEqual(pool.run(0), 8)
            self.assertEqual(pool.run(0), 8)
            self.assertEqual(seen, [9, 10])
