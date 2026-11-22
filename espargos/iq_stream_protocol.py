"""Wire format shared by streaming and scheduled steering (iq_tx_stream_protocol.h)."""

from enum import IntEnum
import struct


class Command(IntEnum):
    OPEN = 1
    DATA = 2
    ARM = 3
    FINISH = 4
    CLOSE = 5
    TABLE = 6
    PACKET = 7
    STEER_LOAD = 8
    STEER_ARM = 9
    STEER_CANCEL = 10
    STEER_PREPARE = 11


VERSION = 4
FEEDBACK_MAGIC = 0x32535449
HEADER = struct.Struct("<IBBBBIII")


def send(board, command, mask, session, offset=0, count=0, payload=b""):
    """Send bulk data over the selected transport and lifecycle commands over HTTP."""
    frame = HEADER.pack(0x31535449, VERSION, command, mask, 0, session, offset, count) + payload
    if command in (Command.DATA, Command.PACKET, Command.FINISH, Command.STEER_LOAD):
        board.send_stream_data(frame)
    else:
        board.control.fetch("iq_tx_stream", frame)
