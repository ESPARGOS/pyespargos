"""Wire format shared by streaming and scheduled steering (iq_tx_stream_protocol.h)."""

from dataclasses import dataclass
from enum import IntEnum
import struct
import threading
import time
import zlib


class Command(IntEnum):
    TXSTREAM_OPEN = 1
    TXSTREAM_DATA = 2
    TXSTREAM_ARM = 3
    TXSTREAM_FINISH = 4
    TXSTREAM_CLOSE = 5
    TXSTREAM_TABLE = 6
    TXSTREAM_PACKET = 7
    STEER_AT = 8
    STEER_CANCEL = 9


VERSION = 6
HEADER = struct.Struct("<IBBBBIII")

TXSTREAM_STATUS = 0x31535854  # TXS1
_STATUS = struct.Struct("<25I")
_STREAM_FIELDS = (
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
    "max_refill_wait_us",
    "max_source_us",
    "max_expand_us",
    "missing",
    "late",
    "table_received",
)
_STREAM_STATES = ("closed", "buffering", "armed", "running", "drained", "fault", "starved")


@dataclass(frozen=True)
class StreamReport:
    antenna: int
    boot_id: int
    sequence: int
    stream_session: int
    state: dict


def decode_status(message):
    data = memoryview(message.payload)
    if len(data) != _STATUS.size:
        return None
    magic, antenna, boot, sequence, *values, crc = _STATUS.unpack(data)
    if magic != TXSTREAM_STATUS or antenna != message.antenna_id or antenna >= 8 or not boot or not values[0] or values[1] >= len(_STREAM_STATES) or crc != zlib.crc32(data[:-4]):
        return None
    state = dict(zip(_STREAM_FIELDS, values))
    state["state"] = _STREAM_STATES[state["state"]]
    if state["end"] == 0xFFFFFFFF:
        state["end"] = None
    state.update(antid=antenna, boot_id=boot, available=True)
    return StreamReport(antenna, boot, sequence, values[0] if values[1] else 0, state)


class Uplink:
    """One UART bandwidth budget per board, shared by samples and steering."""

    def __init__(self, boards):
        self._locks = {board: threading.Lock() for board in boards}
        self._next = {}

    def send(self, board, command, mask, session, offset=0, count=0, payload=b"", *, byte_rate=220_000, min_spacing=0.001):
        frame = HEADER.pack(0x31535449, VERSION, command, mask, 0, session, offset, count) + payload
        # Serialize individual frames so a delayed writer cannot be overtaken
        # between reserving its UART bandwidth and sending the frame.
        with self._locks[board]:
            now = time.monotonic()
            delay = self._next.get(board, now) - now
            if delay > 0:
                time.sleep(delay)
            self._next[board] = time.monotonic() + max(min_spacing, (len(frame) + 32) / byte_rate)
            board.send_stream_data(frame)
