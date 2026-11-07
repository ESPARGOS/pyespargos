"""BLE API and association tests independent of Qt and the camera application."""

import json
import logging
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np

from espargos import (
    Board,
    BLECapability,
    BLEPacket,
    BLECluster,
    BLEReceiver,
    Pool,
    SensorMessage,
    BoardRevisionDensiflorus,
    ClusterCollisionError,
    BLECalibrationError,
)
from espargos.clustering import ClusterAssembler
from espargos.ble_processing import BLEPhase, Waveform, WaveformAlignment, estimate_phase
from test_ble_camera import record


def reference_response(phase):
    from espargos.ble_iq import BLEChannelResponse
    offsets = np.linspace(-.5e6, .5e6, 33)
    response = np.broadcast_to(phase.vector[...,None], (*phase.vector.shape, len(offsets)))
    return BLEChannelResponse(offsets, response, np.ones(response.shape), np.ones(phase.vector.shape))


class FakeBoard(Board):
    def __init__(self, host="test"):
        self.host = host
        self.api_version = (5, 0)
        self.revision = BoardRevisionDensiflorus()
        self._logger = logging.getLogger("test.ble")
        self.callbacks = []
        self.writes = []
        self.config = dict(channel=37, phy="LE1M", gain_mode="manual", rx_gain=40, rf_switch=2, generation=3, reference_source=0, reference_address="00:01:02:03:04:05")
        self.modes = ["ble"] * 8
        self.ble = BLECapability(self)
        self.iq = SimpleNamespace(get_config=lambda: {"modes": self.modes})
        self.control = SimpleNamespace(get_json=lambda path: dict(self.config), post_json=self.post)
        self.closed = False

    def post(self, path, value):
        self.writes.append((path, value))
        self.config.update(value)
        self.config["generation"] += 1
        return json.dumps(self.config)

    def subscribe_sensor_messages(self, header, callback):
        self.callbacks.append(callback)
        return callback

    def unsubscribe_sensor_messages(self, handle):
        self.callbacks.remove(handle)
        return True

    def start(self, **kwargs):
        pass

    def close(self):
        self.closed = True

    def emit(self, antenna=0, **kwargs):
        for callback in self.callbacks[:]:
            callback(SensorMessage(antenna << 29 | 1, antenna, record(**kwargs)))


def message(a=0, **kwargs):
    return SensorMessage(a << 29 | 1, a, BLEPacket(record(**kwargs)))


class ClusterTests(unittest.TestCase):
    def test_mapping_variable_iq_duplicates_and_collisions(self):
        revisions = [BoardRevisionDensiflorus()] * 2
        first = message()
        cluster = BLECluster("event", 1, first.payload, revisions)
        self.assertTrue(cluster.add_message(1, first))
        self.assertFalse(cluster.add_message(1, first))
        self.assertIs(cluster.get_packet(1, 0), first.payload)
        self.assertEqual(cluster.shape, (2, 2, 4))
        self.assertFalse(cluster.completion[0].any())
        for a in range(1, 8):
            cluster.add_message(1, message(a, samples=np.ones(256)))
        self.assertTrue(cluster.is_complete)
        self.assertEqual(len(cluster.get_packet(1, 1).samples), 256)
        with self.assertRaises(ClusterCollisionError):
            cluster.add_message(1, message(0, sequence=2))
        with self.assertRaises(ClusterCollisionError):
            cluster.add_message(0, first)
        self.assertIs(cluster.get_packet(1, 0), first.payload)

    def test_unvalidated_peer_cannot_be_added(self):
        cluster = BLECluster("event", 0, message().payload, [BoardRevisionDensiflorus()])
        with self.assertRaises(ClusterCollisionError):
            cluster.add_message(0, message(1, crc=False))
        self.assertFalse(cluster.completion.any())

    def test_phase_is_separate_and_array_shaped(self):
        cluster = BLECluster("event", 0, message().payload, [BoardRevisionDensiflorus()])
        for a in range(8):
            cluster.add_message(0, message(a))
        with patch("espargos.ble_iq.waveform", return_value=Waveform(np.ones(600,complex), 100, 500, 4, 0)), patch(
            "espargos.ble_iq.align_waveforms", return_value=WaveformAlignment(0, 0.4, 0.99)
        ):
            phase = estimate_phase(cluster)
        self.assertEqual(phase.vector.shape, cluster.shape)
        self.assertTrue(phase.valid.all())
        self.assertTrue(cluster.is_complete)
        cluster.packet.phy = 3
        for a in range(8):
            cluster.get_packet(0,a).phy = 3
        with self.assertRaisesRegex(ValueError, "LE1M, LE2M and LE Coded"):
            estimate_phase(cluster)
        self.assertTrue(cluster.is_complete)  # Raw captures remain usable.


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.board = FakeBoard()
        self.pool = Pool([self.board])
        self.receiver = self.pool.ble
        self.addCleanup(self.pool.close)

    def test_lazy_pool_ownership_and_no_radio_changes(self):
        self.assertIs(self.pool.get_receiver("ble"), self.receiver)
        self.assertIsInstance(self.receiver, ClusterAssembler)
        self.assertEqual(self.board.writes, [])
        self.assertEqual(len(self.board.callbacks), 1)
        self.pool.close()
        self.assertEqual(self.board.callbacks, [])
        self.assertTrue(self.board.closed)
        self.assertEqual(self.board.writes, [])

    def test_raw_subscription_preserves_message_metadata(self):
        seen = []
        handle = self.board.ble.subscribe_packets(seen.append)
        self.board.emit(5, crc=False)
        self.assertIsInstance(seen[0].payload, BLEPacket)
        self.assertEqual(seen[0].antenna_id, 5)
        self.assertFalse(seen[0].payload.crc_valid)
        self.board.ble.unsubscribe(handle)

    def test_independent_discovery_complete_partial_and_duplicate_callbacks(self):
        early, complete, partial = [], [], []
        self.receiver.add_callback(early.append, lambda c: True)
        self.receiver.add_callback(complete.append)
        self.receiver.add_callback(partial.append, include_partial=True)
        self.board.emit(0)
        self.pool.run(0)
        self.assertEqual(len(early), 1)
        self.assertEqual(complete, [])
        for a in range(1, 8):
            self.board.emit(a)
        self.pool.run(0)
        self.assertEqual(len(complete), 1)
        self.assertEqual(len(partial), 1)
        self.board.emit(0)  # Duplicate sequence must not create another event.
        self.pool.run(0)
        self.assertEqual(len(early), 1)
        self.board.emit(0, sequence=2)
        self.pool.run(0)
        self.receiver.settle_timeout = 0
        self.pool.run(0)
        self.assertEqual(len(complete), 1)
        self.assertEqual(len(partial), 2)
        self.assertTrue(partial[-1].settled)
        self.assertFalse(partial[-1].is_complete)

    def test_unqualified_captures_cannot_borrow_an_identity(self):
        seen = []
        self.receiver.add_callback(seen.append)
        for a in range(1, 8):
            self.board.emit(a, crc=False)
        self.board.emit(0)
        self.pool.run(0)
        self.assertEqual(seen, [])
        clusters = self.receiver._get_cluster_cache_snapshot("packets")
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].completion.sum(), 1)

    def test_unknown_connection_crc_is_not_claimed_as_verified(self):
        seen = []
        self.receiver.add_callback(seen.append)
        for antenna in range(8):
            data = bytearray(record())
            data[80:84] = (0x12345678).to_bytes(4, "little")
            data[84:88] = (0xabcdef).to_bytes(4, "little")
            data[88:92] = (1).to_bytes(4, "little")
            packet = BLEPacket(data)
            self.assertTrue(packet.qualified)
            self.assertFalse(packet.crc_valid)
            self.assertIsNone(packet.advertiser)
            self.receiver._add_sensor_message(0, SensorMessage(antenna << 29 | 1, antenna, packet))
        clusters = self.receiver._get_cluster_cache_snapshot("packets")
        self.assertEqual(len(clusters), 1)
        self.assertTrue(clusters[0].is_complete)
        self.assertEqual(clusters[0].crc_valid.sum(), 0)

    def test_random_polarization_clusters_keep_each_sensors_actual_input(self):
        seen = []
        self.receiver.add_callback(seen.append)
        for a in range(8):
            self.board.emit(a, settings=37 | 4 << 8 | 40 << 16, actual_rf=2 + a % 2)
        self.pool.run(0)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].is_complete)
        self.assertEqual(set(seen[0].rf_switch_state.flatten()), {2, 3})
        for a in range(8):
            self.assertEqual(seen[0].get_packet(0, a).rf_switch, 2 + a % 2)

    def test_nonadvertising_packets_are_delivered(self):
        seen = []
        self.receiver.add_callback(seen.append)
        prefix = b"\x03\x0c" + bytes(range(12))
        for a in range(8):
            self.board.emit(a, prefix=prefix)
        self.pool.run(0)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].packet.is_advertisement)

    def test_changed_context_invalidates_calibration_and_pending_events(self):
        self.board.emit(0)
        self.pool.run(0)
        self.receiver._calibrations[0] = object()
        self.board.emit(0, sequence=2, settings=38 | 2 << 8 | 40 << 16)
        self.pool.run(0)
        self.assertFalse(self.receiver.is_calibrated())
        clusters = self.receiver._get_cluster_cache_snapshot("packets")
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].packet.channel, 38)
        with self.assertRaises(BLECalibrationError):
            self.receiver.phase(clusters[0])

    def test_new_generation_continues_raw_delivery_and_rejects_stale_reports(self):
        seen = []
        self.receiver.add_callback(seen.append, lambda c: True)
        self.board.emit(0)
        self.pool.run(0)
        self.receiver._calibrations[0] = object()
        self.board.emit(0, sequence=2, generation=4)
        self.pool.run(0)
        self.assertFalse(self.receiver.is_calibrated())
        self.board.emit(1, sequence=2, generation=3)
        self.pool.run(0)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[-1].packet.generation, 4)
        self.assertEqual(seen[-1].completion.sum(), 1)

    def test_cache_bounds(self):
        self.receiver.max_clusters = 3
        for i in range(10):
            self.board.emit(0, sequence=i + 1)
        self.pool.run(0)
        self.assertEqual(len(self.receiver._get_cluster_cache_snapshot("packets")), 3)
        self.assertEqual(self.receiver.dropped, 7)

    def test_boards_do_not_share_event_identity(self):
        other = FakeBoard("other")
        with Pool([self.board, other]) as pool:
            seen = []
            pool.ble.add_callback(seen.append)
            for a in range(8):
                self.board.emit(a)
                other.emit(a)
            pool.run(0)
            self.assertEqual({c.board_index for c in seen}, {0, 1})
            self.assertTrue(all(c.shape == (2, 2, 4) for c in seen))
            self.assertTrue(all(c.is_complete and c.completion.sum() == 8 for c in seen))


class GainMetadataTests(unittest.TestCase):
    def test_measured_gain_is_distinct_from_configured_gain(self):
        settings = 37 | 2 << 8 | 40 << 16 | 1 << 24
        packet = BLEPacket(record(settings=settings, measured_gain=0))
        self.assertEqual(packet.rx_gain, 0)
        self.assertTrue(packet.gain_reported)
        self.assertIsNone(BLEPacket(record(settings=settings)).rx_gain)
        self.assertEqual(BLEPacket(record()).rx_gain, 40)
        with self.assertRaises(ValueError):
            BLEPacket(record(settings=settings, measured_gain=77))

    def test_phase_and_amplitude_remove_independent_sensor_gains(self):
        from espargos.gain_phase_calibration import GAIN_PHASE_SHIFT_BY_GAIN_RAD
        gains = [0, 15, 35, 43, 49, 55, 65, 75]
        settings = 37 | 2 << 8 | 40 << 16 | 1 << 24
        first = message(settings=settings, measured_gain=gains[0])
        cluster = BLECluster("gain", 0, first.payload, [BoardRevisionDensiflorus()])
        expected = np.zeros(cluster.shape, complex)
        amplitudes = np.zeros(cluster.shape)
        for antenna, gain in enumerate(gains):
            cluster.add_message(0, message(antenna, settings=settings, measured_gain=gain))
            position = cluster.get_sensor_position(0, antenna)
            expected[position] = np.exp(1j * antenna * .13)
            amplitudes[position] = 1 + antenna / 10

        def wave(packet, phy=None, payload_length=None, coding=None):
            antenna = gains.index(packet.rx_gain)
            phase = antenna * .13 + GAIN_PHASE_SHIFT_BY_GAIN_RAD[packet.rx_gain]
            samples = np.full(128, (1 + antenna / 10) * 10**(packet.rx_gain/20) * np.exp(-1j*phase))
            return Waveform(samples, 10, 100, 4, 0)

        def relative(a, b, recovery=False):
            return np.angle(np.vdot(a.samples, b.samples)), 1.

        with patch("espargos.ble_iq.waveform", side_effect=wave), patch("espargos.ble_iq.align_waveforms", return_value=WaveformAlignment(0, 0, 1)):
            phase = estimate_phase(cluster)
        expected *= np.conj(expected.flat[0])
        np.testing.assert_allclose(phase.vector, expected, atol=1e-6)
        np.testing.assert_allclose(phase.amplitude, amplitudes, atol=1e-6)
        cluster.get_packet(0, 7).rx_gain = None
        with patch("espargos.ble_iq.waveform", side_effect=wave), patch("espargos.ble_iq.align_waveforms", return_value=WaveformAlignment(0, 0, 1)):
            partial = estimate_phase(cluster)
        position = cluster.get_sensor_position(0, 7)
        self.assertFalse(partial.valid[position])
        self.assertTrue(np.isnan(partial.amplitude[position]))
        self.assertEqual(partial.valid.sum(), 7)


class PoolExtensionTests(unittest.TestCase):
    def test_additional_receiver_uses_shared_processing_and_cleanup(self):
        Pool.register_receiver("test_packet_capture", BLEReceiver, sensor_mode="ble")
        board = FakeBoard()
        with Pool([board]) as pool:
            ordinary, extra = [], []
            pool.ble.add_callback(ordinary.append)
            extension = pool.get_receiver("test_packet_capture")
            extension.add_callback(extra.append)
            self.assertIs(pool.get_receiver("test_packet_capture"), extension)
            for a in range(8):
                board.emit(a)
            pool.run(0)
            self.assertEqual(len(ordinary), 1)
            self.assertEqual(len(extra), 1)
            self.assertIsNot(ordinary[0], extra[0])
            self.assertEqual(len(board.callbacks), 2)
        self.assertEqual(board.callbacks, [])
        self.assertEqual(board.writes, [])


class CalibrationTests(unittest.TestCase):
    def test_reference_result_applies_only_to_matching_context(self):
        board = FakeBoard()
        with Pool([board]) as pool:
            receiver = pool.ble
            pool.start_processing()

            def reference():
                deadline = time.monotonic() + 1
                while board.config["rf_switch"] != 1 and time.monotonic() < deadline:
                    time.sleep(0.001)
                for seq in range(1, 4):
                    for a in range(8):
                        board.emit(a, sequence=seq * 2 - 1,
                            time_us=100_000 + seq * 6000 - 3000,
                            settings=37 | 1 << 8 | 40 << 16,
                            generation=board.config["generation"],
                            prefix=b"\x00\x06" + bytes.fromhex("ffeeddccbbaa"))
                    for a in range(8):
                        board.emit(
                            a,
                            sequence=seq * 2,
                            time_us=100_000 + seq * 6000,
                            settings=37 | 1 << 8 | 40 << 16,
                            generation=board.config["generation"],
                        )

            producer = threading.Thread(target=reference)
            frequency = 2402e6
            receiver_phase = np.arange(8).reshape(receiver.shape) * 0.17
            ref_phase = receiver_phase - 2 * np.pi * frequency * board.revision.calib_trace_delays
            phase = BLEPhase(np.exp(1j * ref_phase), np.ones(receiver.shape), np.ones(receiver.shape, bool))
            with patch("espargos.ble_calibration.align_packet_iq", return_value=SimpleNamespace(phase=lambda: phase)) as estimate, patch("espargos.ble_calibration.channel_response", return_value=reference_response(phase)):
                producer.start()
                calibration = receiver.calibrate(samples=3, timeout=2)
                producer.join()
            self.assertEqual(estimate.call_count, 3)
            self.assertTrue(receiver.is_calibrated())
            self.assertEqual(board.config["rf_switch"], 2)
            first = message(generation=board.config["generation"])
            cluster = BLECluster("event", 0, first.payload, receiver.board_revisions)
            for a in range(8):
                cluster.add_message(0, message(a, generation=board.config["generation"]))
            self.assertTrue(calibration.matches(cluster))
            arrival_phase = np.arange(8).reshape(receiver.shape) * -0.23
            measured = BLEPhase(np.exp(1j * (receiver_phase + arrival_phase)), np.ones(receiver.shape), np.ones(receiver.shape, bool))
            from espargos.ble_iq import BLEPacketIQ
            iq = BLEPacketIQ(np.repeat(measured.vector[...,None],128,axis=-1),4e6,frequency,2e6,
                             np.zeros(receiver.shape),np.ones(receiver.shape),0,(0,0,0))
            with patch("espargos.ble_iq.align_packet_iq", return_value=iq):
                corrected = receiver.phase(cluster)
                self.assertEqual(corrected.vector.shape, receiver.shape)
                # Recover the incident phase after removing receiver and REF-only propagation.
                np.testing.assert_allclose(corrected.vector / corrected.vector.flat[0], np.exp(1j * arrival_phase), atol=1e-6)
                for route, gain in ((3, 40), (4, 25), (2, 60)):
                    receiver.apply_config({"rf_switch": route, "rx_gain": gain})
                    self.assertTrue(receiver.is_calibrated())
                    settings = 37 | route << 8 | gain << 16
                    changed = BLECluster("changed", 0, message(settings=settings,
                        generation=board.config["generation"], actual_rf=2).payload, receiver.board_revisions)
                    for a in range(8):
                        changed.add_message(0, message(a, settings=settings,
                            generation=board.config["generation"], actual_rf=2))
                    changed_phase = receiver.phase(changed).vector
                    np.testing.assert_allclose(changed_phase / changed_phase.flat[0], np.exp(1j * arrival_phase), atol=1e-6)
                cluster = changed
                cluster.get_packet(0, 3).receiver_epoch += 1
                with self.assertRaises(BLECalibrationError):
                    receiver.phase(cluster)

    def test_capture_changes_preserve_calibration_but_tuning_does_not(self):
        from espargos.ble_calibration import BLECalibration
        for change, preserved in (({"decimation": 8}, True),
                                  ({"channel": 38}, False),
                                  ({"phy": "LE2M"}, False),
                                  ({"expert_gain_words": [1, 2, 3]}, False),
                                  ({"gain_mode": "expert"}, False)):
            with self.subTest(change=change), Pool([FakeBoard()]) as pool:
                receiver = pool.ble
                board = pool.boards[0]
                calibration = BLECalibration(None, 0, 3,
                    board.ble.packet_settings(board.config), 0,
                    {a: (1, 2) for a in range(8)})
                receiver._calibrations[0] = calibration
                receiver.apply_config(change)
                self.assertEqual(receiver.is_calibrated(), preserved)
                if preserved:
                    self.assertIs(receiver._calibrations[0].model, calibration.model)
                    self.assertEqual(receiver._calibrations[0].generation, board.config["generation"])

    def test_preserved_calibration_checks_first_packet_epoch(self):
        from espargos.ble_calibration import BLECalibration
        board = FakeBoard()
        with Pool([board]) as pool:
            receiver = pool.ble
            receiver._calibrations[0] = BLECalibration(None, 0, 3,
                board.ble.packet_settings(board.config), 0,
                {a: (1, 2) for a in range(8)})
            receiver.apply_config({"rx_gain": 55})
            self.assertTrue(receiver.is_calibrated())
            board.emit(0, generation=board.config["generation"],
                       settings=board.ble.packet_settings(board.config), receiver_epoch=3)
            pool.run(0)
            self.assertFalse(receiver.is_calibrated())

    def test_packet_settings_preserves_gain_mode(self):
        config = FakeBoard().config
        for mode, value in (("manual", 0), ("auto", 1), ("expert", 2)):
            config["gain_mode"] = mode
            self.assertEqual(BLECapability.packet_settings(config),
                             37 | 2 << 8 | 40 << 16 | value << 24)

    def test_external_reference_requires_explicit_identity(self):
        board = FakeBoard()
        board.config["reference_source"] = 1
        with Pool([board]) as pool:
            receiver = pool.ble
            pool.start_processing()
            with self.assertRaisesRegex(BLECalibrationError, "Specify reference_address"):
                receiver.calibrate(timeout=0.01)
            self.assertEqual(board.writes, [])
            with self.assertRaisesRegex(BLECalibrationError, "No stable complete"):
                receiver.calibrate(reference_address="AA:BB:CC:DD:EE:FF", timeout=0.01)
            self.assertEqual(board.config["rf_switch"], 2)
            self.assertFalse(receiver.is_calibrated())

    def test_auto_gain_is_restored_after_failed_reference_acquisition(self):
        board = FakeBoard()
        board.config["gain_mode"] = "auto"
        with Pool([board]) as pool:
            receiver = pool.ble
            pool.start_processing()
            with self.assertRaisesRegex(BLECalibrationError, "No stable complete"):
                receiver.calibrate(timeout=0.01)
            self.assertEqual(board.writes[0][1], {"rf_switch": 1, "gain_mode": "manual"})
            self.assertEqual(board.config["gain_mode"], "auto")
            self.assertEqual(board.config["rf_switch"], 2)
            self.assertFalse(receiver.is_calibrated())

    def test_auto_gain_calibration_restores_mode_and_can_be_reused(self):
        board = FakeBoard()
        board.config["gain_mode"] = "auto"
        with Pool([board]) as pool:
            receiver = pool.ble
            pool.start_processing()

            def reference():
                deadline = time.monotonic() + 1
                while board.config["rf_switch"] != 1 and time.monotonic() < deadline:
                    time.sleep(.001)
                for sequence in (1, 2):
                    for antenna in range(8):
                        board.emit(antenna, sequence=sequence, time_us=sequence * 5000,
                            generation=board.config["generation"],
                            settings=board.ble.packet_settings(board.config))

            phase = BLEPhase(np.ones(receiver.shape, complex), np.ones(receiver.shape),
                             np.ones(receiver.shape, bool))
            producer = threading.Thread(target=reference)
            with patch("espargos.ble_calibration.align_packet_iq", return_value=SimpleNamespace(phase=lambda: phase)), patch("espargos.ble_calibration.channel_response", return_value=reference_response(phase)):
                producer.start()
                result = receiver.calibrate(samples=2, timeout=2)
                producer.join()
            self.assertEqual(board.config["gain_mode"], "auto")
            self.assertEqual(result.settings >> 24, 1)
            receiver.apply_config({"gain_mode": "manual"})
            self.assertTrue(receiver.is_calibrated())
            self.assertEqual(receiver._calibrations[0].settings >> 24, 0)

    def test_failed_reference_acquisition_restores_route(self):
        board = FakeBoard()
        with Pool([board]) as pool:
            receiver = pool.ble
            pool.start_processing()
            with self.assertRaises(BLECalibrationError):
                receiver.calibrate(timeout=0.01)
            self.assertEqual(board.config["rf_switch"], 2)
            self.assertFalse(receiver.is_calibrated())
            self.assertEqual(receiver._cluster_callbacks, [])

    def test_restore_failure_cannot_publish_calibration(self):
        board = FakeBoard()
        with Pool([board]) as pool:
            receiver = pool.ble
            pool.start_processing()
            original = board.post

            def post(path, value):
                if value.get("rf_switch") == 2:
                    raise RuntimeError("restore failed")
                return original(path, value)

            board.control.post_json = post
            with self.assertRaisesRegex(RuntimeError, "restore failed"):
                receiver.calibrate(timeout=0.01)
            self.assertFalse(receiver.is_calibrated())
            self.assertEqual(receiver._cluster_callbacks, [])


if __name__ == "__main__":
    unittest.main()


class HostDecodingTests(unittest.TestCase):
    def test_raw_metadata_and_subscription(self):
        import struct
        board = FakeBoard()
        received = []
        board.ble.subscribe_packets(received.append)
        data = bytearray(record())
        struct.pack_into("<I", data, 88, 16 | 3 | 32)
        board.callbacks[0](SensorMessage(1, 0, bytes(data)))
        packet = received[0].payload
        self.assertTrue(packet.raw_capture)
        self.assertFalse(packet.host_decoded)
        self.assertFalse(packet.qualified)
        self.assertFalse(packet.crc_valid)
        self.assertIsNone(packet.identity)
        self.assertIsNone(packet.advertiser)
        self.assertGreater(len(packet.samples), 0)

    def test_host_mode_calibration_attempts_reference_acquisition(self):
        with Pool([FakeBoard()]) as pool:
            receiver = pool.ble
            board = pool.boards[0]
            pool.start_processing()
            try:
                with self.assertRaisesRegex(BLECalibrationError, "No stable complete"):
                    receiver.calibrate(timeout=0.01)
                self.assertTrue(any(value.get("rf_switch") == 1 for _, value in board.writes))
            finally:
                pool.stop_processing()
