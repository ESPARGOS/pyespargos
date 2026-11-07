import time
import unittest
from unittest.mock import patch
import numpy as np
from espargos.ble_phase_history import BLEPhaseHistory
from espargos.array_processing import steering_vectors_2d
from espargos.ble_beamforming import BLEBeamformer, BLEBeamformingConfig


class BacklogTests(unittest.TestCase):
    def test_age_count_and_polarization_pairs(self):
        backlog = BLEPhaseHistory(size=2, max_age=.25)
        vector = np.exp(1j * np.arange(8).reshape(2, 4) * .1)
        with patch('espargos.ble_phase_history.time.monotonic', return_value=10):
            backlog.append(vector, np.full((2,4), 2))
            backlog.append(vector * 1j, np.full((2,4), 3))
            backlog.append(vector * -1, np.full((2,4), 2))
            self.assertEqual(backlog.snapshot_count, 2)
            np.testing.assert_allclose(backlog.covariance(), np.outer(vector, vector.conj()), atol=1e-12)
        with patch('espargos.ble_phase_history.time.monotonic', return_value=10.3):
            self.assertIsNone(backlog.covariance())
            self.assertEqual(backlog.snapshot_count, 0)

    def test_random_inputs_do_not_mix_orthogonal_feed_phases(self):
        backlog = BLEPhaseHistory(size=32, max_age=1)
        vector = np.exp(1j * np.arange(8).reshape(2,4) * .1)
        rng = np.random.default_rng(123)
        for _ in range(32):
            routes = rng.integers(2,4,size=(2,4))
            backlog.append(vector * np.where(routes == 2, 1, np.exp(1.7j)), routes)
        np.testing.assert_allclose(backlog.covariance(), np.outer(vector, vector.conj()), atol=1e-12)

    def test_music_and_roots_follow_new_window(self):
        steering = steering_vectors_2d(2,4,64,48,frequency_hz=2402e6,pixel_centers=True)
        for kind in ('MUSIC','Root-MUSIC'):
            processor = BLEBeamformer()
            config = BLEBeamformingConfig(method=kind, history_size=8)
            for now, target in ((10,(24,20)),(11,(41,27))):
                vector = steering[:,:,target[0],target[1]]
                with patch('espargos.ble_phase_history.time.monotonic', return_value=now):
                    for i in range(8):
                        result = processor.process('target',vector*np.exp(1j*i),
                            np.full((2,4),2),now,steering,2402e6,config)
                if kind == 'Root-MUSIC':
                    self.assertEqual(len(result.directions), 1)
                    expected = (2 * np.asarray(target) + 1) / np.asarray(steering.shape[2:]) - 1
                    np.testing.assert_allclose(result.directions[0], expected, atol=.04)
                else:
                    peak = np.unravel_index(np.argmax(result.spectrum), result.spectrum.shape)
                    self.assertLessEqual(abs(peak[0]-target[0]), 1)
                    self.assertLessEqual(abs(peak[1]-target[1]), 1)



class RawBacklogTests(unittest.TestCase):
    def test_raw_backlog_uses_common_storage_and_detaches(self):
        from espargos import BLEBacklog, Pool
        from espargos.sensor_backlog import SensorBacklog, BacklogFilter
        from test_ble_api import FakeBoard

        class NoReference(BacklogFilter):
            def matches(self, cluster):
                return not cluster.is_calibration

        board = FakeBoard()
        with Pool([board]) as pool:
            backlog = BLEBacklog(pool.ble, size=2)
            self.assertIsInstance(backlog, SensorBacklog)
            backlog.add_filter(NoReference())
            for sequence in range(1, 5):
                for a in range(8):
                    board.emit(a, sequence=sequence, time_us=sequence * 5000,
                        settings=37 | (1 if sequence == 4 else 2) << 8 | 40 << 16)
                pool.run(0)
            packets, completion = backlog.get_multiple(('packets','completion'))
            self.assertEqual(packets.shape, (2,1,2,4))
            self.assertTrue(completion.all())
            self.assertEqual(packets[0,0,0,0].sequence, 2)
            self.assertEqual(packets[1,0,0,0].sequence, 3)
            self.assertEqual(backlog.get('board_index').tolist(), [0,0])
            self.assertEqual(board.writes, [])
            backlog.close()
            backlog.close()
            self.assertEqual(pool.ble._cluster_callbacks, [])

    def test_partial_packets_leave_missing_sensor_slots_empty(self):
        from espargos import BLEBacklog, Pool
        from test_ble_api import FakeBoard
        board = FakeBoard()
        with Pool([board]) as pool:
            backlog = BLEBacklog(pool.ble, callback_predicate=lambda c: True, size=2)
            board.emit(0)
            pool.run(0)
            packets = backlog.get_latest('packets')
            self.assertEqual(sum(p is not None for p in packets.flat), 1)
            self.assertEqual(backlog.get_latest('completion').sum(), 1)
            self.assertEqual(np.isnan(backlog.get_latest('rx_gain')).sum(), 7)
            backlog.close()


    def test_variable_capture_lengths_are_preserved(self):
        from espargos import BLEBacklog, Pool
        from test_ble_api import FakeBoard
        board = FakeBoard()
        with Pool([board]) as pool:
            backlog = BLEBacklog(pool.ble, fields=["packets"], size=2)
            for sequence, length in enumerate((128, 256), 1):
                for antenna in range(8):
                    board.emit(antenna, sequence=sequence, time_us=sequence * 5000,
                               samples=np.full(length, 1+2j))
                pool.run(0)
            packets = backlog.get("packets")
            self.assertEqual(packets.shape, (2, 1, 2, 4))
            self.assertEqual([len(p.samples) for p in packets[:, 0, 0, 0]], [128, 256])
            backlog.close()

    def test_multiple_boards_keep_independent_raw_events(self):
        from espargos import BLEBacklog, Pool
        from test_ble_api import FakeBoard
        boards = [FakeBoard("one"), FakeBoard("two")]
        with Pool(boards) as pool:
            backlog = BLEBacklog(pool.ble, size=2)
            for board in boards:
                for antenna in range(8):
                    board.emit(antenna)
                pool.run(0)
            packets, completion, indices = backlog.get_multiple(("packets", "completion", "board_index"))
            self.assertEqual(indices.tolist(), [0, 1])
            for index in range(2):
                self.assertTrue(completion[index, index].all())
                self.assertFalse(completion[index, 1-index].any())
                self.assertTrue(all(p is None for p in packets[index, 1-index].flat))
            backlog.close()


class PhaseHistoryValidationTests(unittest.TestCase):
    def test_invalid_observations_do_not_poison_history(self):
        history = BLEPhaseHistory()
        with patch('espargos.ble_phase_history.time.monotonic',return_value=10):
            self.assertFalse(history.append(np.full(8,np.nan),np.full(8,255)))
            self.assertFalse(history.append(np.ones(8),np.ones(8)))
            self.assertEqual(history.snapshot_count,0)
            history.append(np.ones(8),np.full(8,2),timestamp=10)
            with self.assertRaises(ValueError):history.append(np.ones(8),np.full(8,2),timestamp=9)
            with self.assertRaises(ValueError):history.append(np.ones(8),np.full(8,2),timestamp=np.nan)
            with self.assertRaises(ValueError):history.append(np.ones((2,4)),np.full((2,4),2))
            with self.assertRaises(ValueError):history.append(np.ones(8),np.full(8,4))
            self.assertEqual(history.snapshot_count,1)
        with self.assertRaises(ValueError):BLEPhaseHistory(size=True)

    def test_covariance_reads_can_overlap_appends(self):
        import threading
        history = BLEPhaseHistory(size=16, max_age=60)
        started = threading.Event()
        failures = []

        def append():
            try:
                started.set()
                for i in range(200):
                    history.append(np.exp(1j * np.arange(8) * i / 100), np.full(8, 2))
            except Exception as error:
                failures.append(error)

        writer = threading.Thread(target=append)
        writer.start()
        self.assertTrue(started.wait(1))
        for _ in range(100):
            covariance = history.covariance()
            if covariance is not None:
                self.assertEqual(covariance.shape, (8, 8))
                self.assertGreaterEqual(np.linalg.eigvalsh(covariance).min(), -1e-10)
        writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(history.snapshot_count, 16)
