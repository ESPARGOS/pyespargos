import binascii
import ctypes
import logging
import queue
import struct
import threading
from collections import OrderedDict
from types import SimpleNamespace

import numpy as np
import pytest

from espargos import csi_compression
from espargos import csi_packet
from espargos import radar_packet
from espargos import revisions
from espargos import sensor
from espargos import wifi
from espargos.backlog import Exclude11bFilter, MacFilter
from espargos.board import Board
from espargos.cluster import CSICluster
from espargos.pool import Pool


def _sensor_fragment(uid, fragment_index, total_fragments, payload):
    return struct.pack("<IHBB", uid, len(payload), fragment_index, total_fragments) + payload


def _sensor_packet(*fragments):
    terminator = struct.pack("<IHBB", sensor.SENSOR_PACKET_TERMINATOR_UID, 0, 0, 0)
    return struct.pack("<I", sensor.SENSOR_PACKET_TYPE_HEADER) + b"".join(fragments) + terminator


def _crc_tlv_packet(type_header, *tlvs):
    without_crc = struct.pack("<I", type_header)
    for tlv_type, value in tlvs:
        without_crc += struct.pack("<BH", tlv_type, len(value)) + value
    return without_crc + struct.pack("<BHI", 255, 4, binascii.crc32(without_crc) & 0xFFFFFFFF)


def _radar_tx_report(tx_count, sequence=23):
    source_mac = bytes.fromhex("001122334455")
    dest_mac = bytes.fromhex("66778899aabb")
    sequence_control = wifi.SequenceControl(struct.pack("<H", sequence << 4))
    raw = _crc_tlv_packet(
        radar_packet.RADAR_TX_REPORT_TYPE_HEADER,
        (
            radar_packet.RADAR_TX_REPORT_TLV_TYPE_FRAME_META,
            source_mac + dest_mac + bytes(sequence_control) + struct.pack("<H", 0),
        ),
        (
            radar_packet.RADAR_TX_REPORT_TLV_TYPE_RADAR_META,
            struct.pack("<IBBxx", tx_count, sensor.RFSwitchState.SENSOR_RFSWITCH_ANTENNA_R, 34),
        ),
    )
    return radar_packet.RadarTxReportPacket(raw)


def _csi_packet(global_timestamp_us, sequence=23, frame_flags=0):
    revision = revisions.BoardRevisionDensiflorus()
    source_mac = bytes.fromhex("001122334455")
    dest_mac = bytes.fromhex("66778899aabb")
    sequence_control = wifi.SequenceControl(struct.pack("<H", sequence << 4))
    rx_ctrl = csi_packet.wifi_pkt_rx_ctrl_v3_t(bytes(ctypes.sizeof(csi_packet.wifi_pkt_rx_ctrl_v3_t)))
    rx_ctrl.channel = 7
    rx_ctrl.cur_bb_format = csi_packet.wifi_rx_bb_format_t.RX_BB_FORMAT_11G
    rx_ctrl.rx_channel_estimate_len = csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL * 2
    rx_ctrl_bytes = ctypes.string_at(ctypes.byref(rx_ctrl), ctypes.sizeof(rx_ctrl))
    raw = _crc_tlv_packet(
        revision.type_header,
        (
            csi_packet.SERIALIZED_CSI_TLV_TYPE_FRAME_META,
            source_mac + dest_mac + bytes(sequence_control) + struct.pack("<H", frame_flags),
        ),
        (
            csi_packet.SERIALIZED_CSI_TLV_TYPE_TIMING_META,
            struct.pack("<Q", global_timestamp_us),
        ),
        (
            csi_packet.SERIALIZED_CSI_TLV_TYPE_ACQUIRE_META,
            struct.pack("<HBB", 0, 2, sensor.RFSwitchState.SENSOR_RFSWITCH_ANTENNA_R),
        ),
        (csi_packet.SERIALIZED_CSI_TLV_TYPE_RX_CTRL_RAW, rx_ctrl_bytes),
        (
            csi_packet.SERIALIZED_CSI_TLV_TYPE_CSI_RAW,
            bytes(csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL * 2),
        ),
    )
    return csi_packet.CSIPacket(raw)


def test_sensor_packet_can_pack_fragments_from_multiple_messages():
    antenna_id = 3
    uid_a = (antenna_id << sensor.SENSOR_UID_ANTENNA_SHIFT) | 41
    uid_b = (antenna_id << sensor.SENSOR_UID_ANTENNA_SHIFT) | 42
    packet = sensor.SensorPacket.from_bytes(
        _sensor_packet(
            _sensor_fragment(uid_a, 0, 2, b"first-"),
            _sensor_fragment(uid_b, 0, 1, b"other"),
            _sensor_fragment(uid_a, 1, 2, b"message"),
        )
    )

    assert packet.antenna_id == antenna_id
    assert [fragment.uid for fragment in packet.fragments] == [uid_a, uid_b, uid_a]
    assert [fragment.payload for fragment in packet.fragments] == [b"first-", b"other", b"message"]
    assert packet.fragments[0].to_header().size == len(b"first-")


def test_sensor_packet_rejects_fragments_from_different_sensors():
    uid_a = (1 << sensor.SENSOR_UID_ANTENNA_SHIFT) | 1
    uid_b = (2 << sensor.SENSOR_UID_ANTENNA_SHIFT) | 2

    with pytest.raises(ValueError, match="multiple sensors"):
        sensor.SensorPacket.from_bytes(
            _sensor_packet(
                _sensor_fragment(uid_a, 0, 1, b"a"),
                _sensor_fragment(uid_b, 0, 1, b"b"),
            )
        )


def test_sensor_message_reassembly_is_uid_keyed_and_out_of_order():
    antenna_id = 5
    uid = (antenna_id << sensor.SENSOR_UID_ANTENNA_SHIFT) | 123
    reassembler = sensor.SensorMessageReassembler()

    assert reassembler.push(sensor.SensorPacket.from_bytes(_sensor_packet(_sensor_fragment(uid, 1, 2, b"world")))) == []
    completed = reassembler.push(sensor.SensorPacket.from_bytes(_sensor_packet(_sensor_fragment(uid, 0, 2, b"hello "))))

    assert completed == [sensor.SensorMessage(uid=uid, antenna_id=antenna_id, payload=b"hello world")]


def test_sensor_message_reassembly_discards_timed_out_partial_message():
    uid = (2 << sensor.SENSOR_UID_ANTENNA_SHIFT) | 77
    reassembler = sensor.SensorMessageReassembler(timeout_s=1.0)

    reassembler.push(sensor.SensorPacket.from_bytes(_sensor_packet(_sensor_fragment(uid, 0, 2, b"old"))), now=0.0)
    completed = reassembler.push(sensor.SensorPacket.from_bytes(_sensor_packet(_sensor_fragment(uid, 1, 2, b"late"))), now=2.0)

    assert completed == []


def test_board_dispatches_raw_sensor_messages_to_matching_callbacks():
    logical_type = 0x12345678
    antenna_id = 6
    uid = (antenna_id << sensor.SENSOR_UID_ANTENNA_SHIFT) | 91

    board = Board.__new__(Board)
    board.logger = logging.getLogger("test-board")
    board._sensor_message_reassembler = sensor.SensorMessageReassembler()
    board._sensor_message_subscriptions = {}
    board._sensor_message_subscriptions_lock = threading.Lock()
    received = []
    board.subscribe_sensor_messages(
        logical_type,
        received.append,
    )

    payload = struct.pack("<I", logical_type) + b"payload"
    board._csistream_handle_message(_sensor_packet(_sensor_fragment(uid, 0, 1, payload)))

    assert received == [
        sensor.SensorMessage(
            uid=uid,
            antenna_id=antenna_id,
            payload=payload,
        )
    ]


def test_sensor_message_callbacks_own_decoding_filtering_and_lifetime():
    logical_type = 0x32435149
    antenna_id = 2
    uid = (antenna_id << sensor.SENSOR_UID_ANTENNA_SHIFT) | 92

    class DecodedPacket:
        calls = 0

        def __init__(self, raw):
            type(self).calls += 1
            self.raw = bytes(raw)

    board = Board.__new__(Board)
    board.logger = logging.getLogger("test-board-subscriptions")
    board._sensor_message_reassembler = sensor.SensorMessageReassembler()
    board._sensor_message_subscriptions = {}
    board._sensor_message_subscriptions_lock = threading.Lock()
    received_a = []
    received_b = []
    received_other_type = []

    subscription_a = board.subscribe_sensor_messages(
        logical_type,
        received_a.append,
    )

    def decode_for_b(message):
        received_b.append(sensor.decode_sensor_message(message, DecodedPacket))

    subscription_b = board.subscribe_sensor_messages(
        logical_type,
        decode_for_b,
    )
    board.subscribe_sensor_messages(logical_type + 1, received_other_type.append)

    payload = struct.pack("<I", logical_type) + b"payload"
    board._csistream_handle_message(_sensor_packet(_sensor_fragment(uid, 0, 1, payload)))

    assert DecodedPacket.calls == 1
    assert received_a[0].payload == payload
    assert received_b[0].payload.raw == payload
    assert received_a[0].uid == received_b[0].uid == uid
    assert received_other_type == []
    assert board.unsubscribe_sensor_messages(subscription_a)
    assert not board.unsubscribe_sensor_messages(subscription_a)
    assert board.unsubscribe_sensor_messages(subscription_b)
    assert logical_type not in board._sensor_message_subscriptions


def test_pool_registers_callbacks_and_owns_csi_and_radar_decoding():
    class Revision:
        type_header = 0x12345678
        antid_to_esp_num = {2: 5}

        class serialized_csi_t:
            def __init__(self, raw):
                self.raw = bytes(raw)

    class FakeBoard:
        revision = Revision()

        def __init__(self):
            self.subscriptions = []

        def subscribe_sensor_messages(self, type_header, callback):
            subscription = (type_header, callback)
            self.subscriptions.append(subscription)
            return subscription

    board = FakeBoard()
    pool = Pool([board])

    assert len(board.subscriptions) == 2
    assert [subscription[0] for subscription in board.subscriptions] == [
        Revision.type_header,
        radar_packet.RADAR_TX_REPORT_TYPE_HEADER,
    ]
    assert all(callable(subscription[1]) for subscription in board.subscriptions)

    raw_message = sensor.SensorMessage(
        uid=(2 << sensor.SENSOR_UID_ANTENNA_SHIFT) | 7,
        antenna_id=2,
        payload=struct.pack("<I", Revision.type_header) + b"csi",
    )
    board.subscriptions[0][1](raw_message)

    esp_num, decoded_message, board_num = pool._input_queue.get_nowait()
    with pytest.raises(queue.Empty):
        pool._input_queue.get_nowait()
    assert esp_num == 5
    assert board_num == 0
    assert decoded_message.uid == raw_message.uid
    assert decoded_message.antenna_id == 2
    assert decoded_message.payload.raw == raw_message.payload


def test_pool_run_processes_queued_batch_without_qsize():
    class QueueWithoutQsize(queue.Queue):
        def qsize(self):
            raise AssertionError("Pool must not use advisory Queue.qsize()")

    pool = Pool.__new__(Pool)
    pool._input_queue = QueueWithoutQsize()
    pool._run_lock = threading.Lock()
    pool._input_queue.put("first")
    pool._input_queue.put("second")
    pool._input_queue.put("third")
    handled = []
    checks = []
    pool._handle_packet = handled.append
    pool._check_ota_clusters = lambda: checks.append(True)

    assert pool.run() == 3

    assert handled == ["first", "second", "third"]
    assert checks == [True]
    with pytest.raises(queue.Empty):
        pool._input_queue.get_nowait()


def test_pool_drops_conflicting_radar_report_and_retains_cluster(caplog):
    revision = revisions.BoardRevisionDensiflorus()
    pool = Pool.__new__(Pool)
    pool.boards = [SimpleNamespace(revision=revision)]
    pool.cluster_cache_ota = OrderedDict()
    pool.cluster_cache_ota_lock = threading.Lock()
    pool.callbacks = []
    pool.logger = logging.getLogger("test.pool")
    pool._frame_collisions_since_warning = 0
    pool._last_frame_collision_warning = None

    first_report = _radar_tx_report(tx_count=17)
    second_report = _radar_tx_report(tx_count=17 + 4096)
    with caplog.at_level(logging.WARNING, logger="test.pool"):
        pool._handle_packet((0, SimpleNamespace(payload=first_report), 0))
        first_cluster = next(iter(pool.cluster_cache_ota.values()))
        pool._handle_packet((0, SimpleNamespace(payload=second_report), 0))

    second_cluster = next(iter(pool.cluster_cache_ota.values()))

    assert second_cluster is first_cluster
    assert second_cluster.get_radar_tx_info().tx_count == 17
    assert len(pool.cluster_cache_ota) == 1
    assert len(caplog.records) == 1
    assert "sequence=23" in caplog.text
    assert "tx_count=4113" in caplog.text
    assert "dropping the incoming message" in caplog.text


def test_pool_drops_conflicting_csi_and_ignores_exact_duplicate(caplog):
    revision = revisions.BoardRevisionDensiflorus()
    pool = Pool.__new__(Pool)
    pool.boards = [SimpleNamespace(revision=revision)]
    pool.cluster_cache_ota = OrderedDict()
    pool.cluster_cache_ota_lock = threading.Lock()
    pool.callbacks = []
    pool.logger = logging.getLogger("test.pool.csi")
    pool._frame_collisions_since_warning = 0
    pool._last_frame_collision_warning = None

    first_csi = _csi_packet(global_timestamp_us=1000)
    conflicting_csi = _csi_packet(global_timestamp_us=2000)
    with caplog.at_level(logging.WARNING, logger="test.pool.csi"):
        pool._handle_packet((0, SimpleNamespace(payload=first_csi), 0))
        first_cluster = next(iter(pool.cluster_cache_ota.values()))
        pool._handle_packet((0, SimpleNamespace(payload=first_csi), 0))
        pool._handle_packet((0, SimpleNamespace(payload=conflicting_csi), 0))

    retained_cluster = next(iter(pool.cluster_cache_ota.values()))
    row, col = revision.esp_num_to_row_col(0)

    assert retained_cluster is first_cluster
    assert retained_cluster.serialized_csi_all[0][row][col] is first_csi
    assert len(caplog.records) == 1
    assert "CSI from board 0, sensor 0" in caplog.text
    assert "dropping the incoming message" in caplog.text


def test_csi_and_radar_packets_use_the_same_wifi_frame_key():
    csi = _csi_packet(global_timestamp_us=1000)
    report = _radar_tx_report(tx_count=17)

    assert wifi.WiFiFrameKey.from_packet(csi) == wifi.WiFiFrameKey.from_packet(report)


def test_pool_drops_cross_type_frame_key_collision(caplog):
    revision = revisions.BoardRevisionDensiflorus()
    pool = Pool.__new__(Pool)
    pool.boards = [SimpleNamespace(revision=revision)]
    pool.cluster_cache_ota = OrderedDict()
    pool.cluster_cache_ota_lock = threading.Lock()
    pool.callbacks = []
    pool.logger = logging.getLogger("test.pool.cross_type")
    pool._frame_collisions_since_warning = 0
    pool._last_frame_collision_warning = None

    csi = _csi_packet(global_timestamp_us=1000)
    report = _radar_tx_report(tx_count=17)
    with caplog.at_level(logging.WARNING, logger="test.pool.cross_type"):
        pool._handle_packet((0, SimpleNamespace(payload=csi), 0))
        pool._handle_packet((0, SimpleNamespace(payload=report), 0))

    retained_cluster = next(iter(pool.cluster_cache_ota.values()))

    assert not retained_cluster.has_radar_tx_report()
    assert len(caplog.records) == 1
    assert "radar TX report" in caplog.text


def test_pool_rejects_unknown_decoded_message_type():
    pool = Pool.__new__(Pool)

    with pytest.raises(TypeError, match="Unsupported Pool sensor-message payload"):
        pool._handle_packet((0, SimpleNamespace(payload=object()), 0))


def test_backlog_filters_still_operate_on_clusters_after_module_rename():
    class Cluster:
        def get_source_mac(self):
            return "001122aabbcc"

        def is_11b(self):
            return False

    cluster = Cluster()

    assert MacFilter(r"^001122").matches(cluster)
    assert Exclude11bFilter().matches(cluster)


def test_csi_packet_expands_compressed_receive_metadata():
    compact = csi_compression.CompressedRxControl(bytes(ctypes.sizeof(csi_compression.CompressedRxControl)))
    compact.rssi = 0xD8
    compact.noise_floor = 0xA2
    compact.channel = 44
    compact.secondary_channel = 1
    compact.cur_bb_format = csi_packet.wifi_rx_bb_format_t.RX_BB_FORMAT_HT
    compact.rate = 7
    compact.sig_mode = csi_packet.wifi_sig_mode_t.SIG_MODE_HT
    compact.rxstart_time_cyc = 53
    compact.rx_channel_estimate_len = 128
    compact.flags = (
        csi_compression.RX_CONTROL_FLAG_IS_HT40
        | csi_compression.RX_CONTROL_FLAG_CHANNEL_ESTIMATE_INFO_VALID
    )
    compact.timestamp = 0x12345678
    compact.fft_gain = 0xF9
    compact.rx_gain = 38
    compact_bytes = ctypes.string_at(ctypes.byref(compact), ctypes.sizeof(compact))
    compressed_csi = b"\x03" + bytes(range(csi_compression.COMPRESSED_TAP_COUNT * 4))
    raw = _crc_tlv_packet(
        0xE4CD0BAC,
        (csi_packet.SERIALIZED_CSI_TLV_TYPE_RX_CTRL_COMPRESSED, compact_bytes),
        (csi_packet.SERIALIZED_CSI_TLV_TYPE_CSI_COMPRESSED, compressed_csi),
    )

    packet = csi_packet.CSIPacket(raw)
    rx_ctrl = csi_packet.wifi_pkt_rx_ctrl_v3_t(packet.rx_ctrl)

    assert packet.is_compressed
    assert packet.buf == compressed_csi
    assert packet.csi_len == 1 + csi_compression.COMPRESSED_TAP_COUNT * 4
    assert rx_ctrl.timestamp == 0x12345678
    assert rx_ctrl.channel == 44
    assert rx_ctrl.second == 1
    assert rx_ctrl.he_siga1 & 0x80
    assert rx_ctrl.rx_channel_estimate_info_vld == 1


def test_csi_packets_still_form_and_deserialize_a_complete_cluster():
    source_mac = bytes.fromhex("001122334455")
    dest_mac = bytes.fromhex("66778899aabb")
    sequence_control = wifi.SequenceControl(struct.pack("<H", (23 << 4) | 1))
    revision = revisions.BoardRevisionDensiflorus()
    csi_cluster = CSICluster(
        source_mac.hex(),
        dest_mac.hex(),
        sequence_control,
        [revision],
    )

    for esp_num in range(8):
        rx_ctrl = csi_packet.wifi_pkt_rx_ctrl_v3_t(bytes(ctypes.sizeof(csi_packet.wifi_pkt_rx_ctrl_v3_t)))
        rx_ctrl.rssi = 0xD8
        rx_ctrl.noise_floor = 0xA2
        rx_ctrl.channel = 7
        rx_ctrl.cur_bb_format = csi_packet.wifi_rx_bb_format_t.RX_BB_FORMAT_11G
        rx_ctrl.rx_channel_estimate_len = csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL * 2
        rx_ctrl.timestamp = 1000 + esp_num
        rx_ctrl_bytes = ctypes.string_at(ctypes.byref(rx_ctrl), ctypes.sizeof(rx_ctrl))
        frame_meta = source_mac + dest_mac + bytes(sequence_control) + struct.pack("<H", 0)
        timing_meta = struct.pack("<Q", 5000 + esp_num)
        acquire_meta = struct.pack("<HBB", 0, 2, sensor.RFSwitchState.SENSOR_RFSWITCH_ANTENNA_R)
        raw = _crc_tlv_packet(
            revision.type_header,
            (csi_packet.SERIALIZED_CSI_TLV_TYPE_FRAME_META, frame_meta),
            (csi_packet.SERIALIZED_CSI_TLV_TYPE_TIMING_META, timing_meta),
            (csi_packet.SERIALIZED_CSI_TLV_TYPE_ACQUIRE_META, acquire_meta),
            (csi_packet.SERIALIZED_CSI_TLV_TYPE_RX_CTRL_RAW, rx_ctrl_bytes),
            (
                csi_packet.SERIALIZED_CSI_TLV_TYPE_CSI_RAW,
                bytes(csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL * 2),
            ),
        )
        packet = csi_packet.CSIPacket(raw)
        packet.antid = next(antid for antid, mapped_esp_num in revision.antid_to_esp_num.items() if mapped_esp_num == esp_num)
        csi_cluster.add_csi(0, esp_num, packet)

    csi_values = csi_cluster.deserialize_csi_lltf()

    assert csi_cluster.get_completion_all()
    assert csi_values.shape == (1, 2, 4, csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL)
    assert np.isfinite(csi_values).all()


def test_radar_packet_parsing_is_independent_of_csi():
    raw = _crc_tlv_packet(
        radar_packet.RADAR_TX_REPORT_TYPE_HEADER,
        (
            radar_packet.RADAR_TX_REPORT_TLV_TYPE_RADAR_META,
            struct.pack("<IBBxx", 17, sensor.RFSwitchState.SENSOR_RFSWITCH_REFERENCE, 33),
        ),
    )

    packet = radar_packet.RadarTxReportPacket(raw)

    assert packet.tx_count == 17
    assert packet.rfswitch_state == sensor.RFSwitchState.SENSOR_RFSWITCH_REFERENCE
    assert packet.tx_power == 33
