import numpy as np
import pytest

from espargos.sensor_backlog import BacklogField, SensorBacklog


def test_incremental_reads_follow_insertions_across_wrap_resize_and_clear():
    backlog = SensorBacklog((1, 2, 4), {"time": BacklogField((), np.int64, False, 0)}, size=3)
    backlog.append_datapoint({"time": 10})
    cursor = backlog.total_appended
    for stamp in (9, 12):
        backlog.append_datapoint({"time": stamp})
    cursor, (rows,) = backlog.get_since(["time"], cursor)
    assert rows.tolist() == [9, 12]
    rows[:] = 0
    assert backlog.get("time").tolist() == [10, 9, 12]

    for stamp in (12, 11, 14, 13):
        backlog.append_datapoint({"time": stamp})
    backlog.size = 2
    cursor, (rows,) = backlog.get_since(["time"], cursor)
    assert rows.tolist() == [14, 13]
    assert not backlog.get_since(["time"], cursor)[1][0].size
    backlog.clear()
    backlog.append_datapoint({"time": 8})
    assert backlog.get_since(["time"], cursor)[1][0].tolist() == [8]
    with pytest.raises(ValueError, match="cursor"):
        backlog.get_since(["time"], cursor + 2)


def test_failed_resize_preserves_current_snapshot_and_cursor(monkeypatch):
    backlog = SensorBacklog((1, 2, 4), {"x": BacklogField((), np.int64, False, 0)}, size=2)
    backlog.append_datapoint({"x": 7})

    def out_of_memory(size, spec):
        raise MemoryError

    monkeypatch.setattr(backlog, "_allocate_field", out_of_memory)
    with pytest.raises(MemoryError):
        backlog.size = 10
    assert backlog.size == 2
    assert backlog.total_appended == 1
    assert backlog.get("x").tolist() == [7]
