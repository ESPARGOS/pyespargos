"""Exercise per-sensor slot mapping and the common sample clock on two controllers."""

import struct
from threading import RLock
from types import SimpleNamespace

import numpy as np
import pytest

from espargos.calibration import Antenna, AntennaCorrection, Calibration, CalibrationGroup, CalibrationState
from espargos.iq_transmitter import IQTransmitter
from espargos.iq_stream import IQStream
from espargos.iq_stream_protocol import Command, HEADER
from espargos.iq_steering_schedule import SteeringSchedule
from espargos.steering import Beamspace, Weights
from espargos.stream_sources import Stream
from espargos.tx_signal import TXSignal, TXSignals, _SignalSettings
from espargos.waveforms import CW


class Board:
    def __init__(self, host, epoch):
        self.host = host
        self.state = {"sensors": [dict(enabled=True, applied_sample_rate_hz=80_000_000,
                                       frequency_hz=2440000000, sync_seq=8, sample_position=2**35, epoch=epoch + a,
                                       boot_id=99, applied_generation=31, steer_id=0, steer_error=0, steer_applied=0)
                                  for a in range(8)]}
        self.control = SimpleNamespace(fetch=lambda _, frame: self.send_stream_data(frame),
                                       get_json=lambda _: self.state)
        self.sessions = {}
        self.points = [[] for _ in range(8)]
        self.arms = []
        self.drop = False

    def send_stream_data(self, frame):
        _, _, command, mask, _, session, offset, count = HEADER.unpack_from(frame)
        data = frame[HEADER.size:]
        for a, s in enumerate(self.state["sensors"]):
            if not mask & (1 << a):
                continue
            if command == Command.STEER_LOAD:
                if self.drop:
                    s.update(steer_id=session, steer_loaded=0, steer_error=1)
                    continue
                assert offset == len(self.points[a])
                self.points[a].extend(struct.iter_unpack("<IIff", data))
                s.update(steer_id=session, steer_loaded=offset + count, steer_state=0)
            elif command == Command.STEER_PREPARE:
                assert count == len(self.points[a])
                assert struct.unpack("<I", data)[0] == self.sessions.get(a, 0)
                s["steer_state"] = 1
            elif command == Command.STEER_ARM:
                start, seq, *epochs, stream = struct.unpack("<QI8II", data)
                assert epochs[a] == s["epoch"] and seq == s["sync_seq"]
                assert stream == self.sessions.get(a, 0)
                self.arms.append(start)
                s.update(steer_state=2, steer_applied=count)
            elif command == Command.STEER_CANCEL and s["steer_id"] == session:
                s.update(steer_id=0, steer_state=0)


def fixture():
    boards = [Board("left", 11), Board("right", 32)]
    tx = SimpleNamespace(closed=False, boards=boards, owner=SimpleNamespace(_lock=RLock()),
                         _stream=None, _schedules=set())
    def wait(board, predicate, timeout, *, antennas):
        assert predicate(board.state)
        return board.state
    tx.capture = SimpleNamespace(wait=wait)
    tx._parallel = lambda f, boards: [f(i, b) for i, b in enumerate(tx.boards) if b in boards]
    first, second = Antenna("left", 1), Antenna("right", 4)
    narrow = TXSignal(None, _SignalSettings(CW(), .2, (first, second), None))
    pilot = TXSignal(None, _SignalSettings(CW(), .1, (second,), None))
    tx.signals = TXSignals(tx)
    tx.signals._items.extend([pilot, narrow])
    return tx, narrow, pilot, (first, second)


def test_shared_sample_different_local_slots():
    tx, beam, pilot, (a, b) = fixture()
    target = Weights({a: 1j, b: -1})
    with SteeringSchedule(tx, [(23, beam, target), (23, pilot, Weights({b: 1}))],
                          start_sample=2**35 + 80_000_000) as schedule:
        assert tx.boards[0].arms == tx.boards[1].arms == [schedule.start_sample]
        assert tx.boards[0].points[1][0] == pytest.approx((23, 0, 0, .2))
        assert tx.boards[1].points[4][0] == pytest.approx((23, 0, .1, 0))
        assert tx.boards[1].points[4][1] == pytest.approx((23, 1, -.2, 0))
        schedule.wait()
        assert beam.steering is target


def test_missing_chunk_never_arms_other_board():
    tx, beam, _, _ = fixture()
    tx.boards[1].drop = True
    with pytest.raises(RuntimeError, match="steering error"):
        SteeringSchedule(tx, [(1, beam, Weights({}))])
    assert all(not b.arms for b in tx.boards)


def test_limit_and_sync_checked_before_upload():
    tx, beam, _, _ = fixture()
    with pytest.raises(ValueError, match="256"):
        SteeringSchedule(tx, [(i, beam, Weights({})) for i in range(257)])
    tx.boards[1].state["sensors"][4]["sync_seq"] += 1
    with pytest.raises(ValueError, match="common calibrated"):
        SteeringSchedule(tx, [(1, beam, Weights({}))])
    assert all(not any(b.points) for b in tx.boards)


def test_independent_schedules_can_overlap_on_different_clocks():
    tx, _, right_beam, (a, _) = fixture()
    left, right = tx.boards
    left_beam = TXSignal(None, _SignalSettings(CW(), .1, (a,), None))
    tx.signals._items.append(left_beam)
    for sensor in left.state["sensors"]:
        sensor.update(sync_seq=31, applied_sample_rate_hz=40_000_000, sample_position=10_000)
    read_left = left.control.get_json
    left.control.get_json = lambda _: pytest.fail("Unrelated board was polled")
    with SteeringSchedule(tx, [(32, right_beam, Weights({}))]) as sweep_right:
        left.control.get_json = read_left
        with SteeringSchedule(tx, [(17, left_beam, Weights({}))]) as sweep_left:
            assert sweep_left.start_sample < sweep_right.start_sample
            assert set(sweep_left.wait()) == {left}
        assert set(sweep_right.wait()) == {right}


def test_board_and_sensor_specific_stream_sessions():
    tx, beam, _, (a, b) = fixture()
    for board in tx.boards:
        for sensor in board.state["sensors"]:
            sensor["applied_sample_rate_hz"] = 16_000_000
    # A cyclic beam spans both boards. Only one antenna is also playing live data.
    stream = IQStream.__new__(IQStream)
    stream.closed, stream.started, stream.session = False, True, 42
    stream.antennas, stream._amplitude = (a,), .1
    tx._stream = stream
    tx.boards[0].sessions[a.antid] = stream.session
    live_beam = TXSignal(tx.signals, _SignalSettings(Stream(), .1, (a,), None))
    tx.signals._items.insert(0, live_beam)
    stream.signal = live_beam
    # Include a second cyclic-only antenna on the stream's own controller.
    c = Antenna(a.host, 5)
    third = TXSignal(None, _SignalSettings(CW(), .1, (c,), None))
    tx.signals._items.append(third)
    loop_weights = Weights({b: -.5j})
    stream_weights = Weights({a: .3 - .4j})
    uniform = Weights({})
    changes = [(64, beam, loop_weights), (64, live_beam, stream_weights),
               (96, stream, Weights({a: 0})), (128, stream, uniform),
               (7, third, Weights({})), (9, third, Weights({}))]
    with IQTransmitter.schedule_steering(tx, changes) as sweep:
        assert all(start == sweep.start_sample for board in tx.boards for start in board.arms)
        left, right = tx.boards
        assert left.points[a.antid] == [
            pytest.approx((64, 0, .2, 0)),       # Unspecified loop antenna has unit weight.
            pytest.approx((64, 4, .03, -.04)),  # Live amplitude is .1.
            (96, 4, 0, 0),
            pytest.approx((128, 4, .1, 0)),
        ]
        assert right.points[b.antid] == [pytest.approx((64, 1, 0, -.1))]
        sweep.wait()
        assert beam.steering is loop_weights
        assert stream.steering is live_beam.steering is uniform


def test_unstarted_stream_on_another_array_does_not_constrain_loop():
    tx, _, right_beam, (a, _) = fixture()
    tx._stream = SimpleNamespace(closed=False, started=False, antennas=(a,))
    with SteeringSchedule(tx, [(5, right_beam, Weights({}))], start_sample=2**35 + 80000001) as sweep:
        sweep.wait()


def test_overlap_rejected_before_any_upload_or_cancel():
    tx, beam, _, (a, _) = fixture()
    sensor = tx.boards[0].state["sensors"][a.antid]
    sensor.update(steer_id=1234, steer_state=2, steer_loaded=8, steer_applied=3)
    with pytest.raises(RuntimeError, match="another steering schedule"):
        SteeringSchedule(tx, [(64, beam, Weights({}))])
    assert sensor["steer_id"] == 1234
    assert all(not any(b.points) for b in tx.boards)


def test_synchronized_beams_only_need_coherence_across_their_own_antennas():
    tx, combined, right, (a, b) = fixture()
    left = TXSignal(tx.signals, _SignalSettings(CW(), .1, (a,), None))
    tx.signals._items.append(left)
    tx.positions = np.zeros((2, 8, 2))
    groups = []
    for board, antenna in zip(tx.boards, (a, b)):
        sensor = board.state["sensors"][antenna.antid]
        sensor.update(antid=antenna.antid, applied_mode="iq_tx", origin_ns=100, samples=1024)
        state = CalibrationState.from_tx_status(board.host, sensor)
        groups.append(CalibrationGroup((AntennaCorrection(state, 0, 0),)))
    tx.owner.calibrations = Calibration(tuple(groups))
    direction = Beamspace(azimuth_deg=20)
    with pytest.raises(ValueError, match="independent calibration references"):
        SteeringSchedule(tx, [(0, combined, direction)])
    with SteeringSchedule(tx, [(0, left, direction), (0, right, direction)]) as sweep:
        sweep.wait()
        assert tx.boards[0].arms == tx.boards[1].arms == [sweep.start_sample]
