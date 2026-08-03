"""Sensor-stream framing and reassembly.

Sensor messages vary considerably in size. Sending every message as a separate
SPI and network transfer would waste bandwidth on framing overhead and
partially filled transport buffers. Some messages may also be larger than a
single transport packet.

The sensor stream therefore uses two complementary mechanisms:

1. A message is divided into numbered fragments when necessary.
2. Fragments are packed together into sensor packets so that the fixed-size
   sensor-to-controller transport is used efficiently. One sensor packet may
   carry fragments from several messages produced by the same sensor, and one
   message may span several sensor packets.

The controller deliberately does not reassemble messages. It validates the
packet framing and sensor identity, removes unused transport padding, and
forwards the fragments to the host. This keeps the controller firmware
lightweight and avoids maintaining per-message reassembly state there.

Before fragmentation, each logical message is assigned a 32-bit UID. For
sensor-originated messages, the upper three bits encode the antenna ID and the
lower 29 bits are a rolling per-sensor message counter. Every fragment of the
same message carries the same UID, allowing the host to distinguish interleaved
messages and recover their originating sensor. Counter value zero is skipped,
and UID zero is reserved for the fragment-list terminator in a sensor packet.
The UID is only a transport-level reassembly key: the counter restarts with the
sensor firmware and eventually wraps, so it is not a persistent measurement or
Wi-Fi frame identifier.

The host groups fragments by this UID and joins them in fragment-index order.
Once all fragments have arrived, their payload can be decoded into a complete
sensor message.

Terminology:

- SensorPacket:
  A transport packet produced by one sensor. It contains zero or more
  SensorFragments, all originating from that sensor.

- SensorFragment:
  A numbered portion of one logical message. Its UID identifies both the
  originating sensor and the message to which it belongs.

- SensorMessage:
  A complete logical message produced by host-side fragment reassembly. Its
  payload may be raw bytes or a decoded message such as CSI data or a radar TX
  report.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Callable, Generic, Iterable, TypeVar

__all__ = [
    "RFSwitchState",
    "SENSOR_PACKET_TYPE_HEADER",
    "SensorFragment",
    "SensorFragmentHeader",
    "SensorMessage",
    "SensorMessageReassembler",
    "SensorPacket",
    "decode_sensor_message",
    "get_sensor_message_type_header",
    "uid_to_antenna_id",
]

SENSOR_PACKET_TYPE_HEADER = 0xDECAFBAD
SENSOR_PACKET_TERMINATOR_UID = 0
SENSOR_UID_ANTENNA_SHIFT = 29
SENSOR_UID_ANTENNA_MASK = 0x7


class RFSwitchState(IntEnum):
    SENSOR_RFSWITCH_ISOLATION = 0
    SENSOR_RFSWITCH_REFERENCE = 1
    SENSOR_RFSWITCH_ANTENNA_R = 2
    SENSOR_RFSWITCH_ANTENNA_L = 3
    SENSOR_RFSWITCH_ANTENNA_RANDOM = 4
    SENSOR_RFSWITCH_UNKNOWN = 255


class SensorFragmentHeader(ctypes.LittleEndianStructure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = [
        ("uid", ctypes.c_uint32),
        ("size", ctypes.c_uint16),
        ("fragment_index", ctypes.c_uint8),
        ("total_fragments", ctypes.c_uint8),
    ]

    def __new__(cls, buf=None):
        return cls.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass


assert ctypes.sizeof(SensorFragmentHeader) == 8


def uid_to_antenna_id(uid: int) -> int:
    """Extract the originating antenna ID encoded in a message UID."""

    return (int(uid) >> SENSOR_UID_ANTENNA_SHIFT) & SENSOR_UID_ANTENNA_MASK


@dataclass(frozen=True)
class SensorFragment:
    uid: int
    fragment_index: int
    total_fragments: int
    payload: bytes

    @property
    def antenna_id(self) -> int:
        return uid_to_antenna_id(self.uid)

    @property
    def size(self) -> int:
        return len(self.payload)

    def to_header(self) -> SensorFragmentHeader:
        header = SensorFragmentHeader(bytes(ctypes.sizeof(SensorFragmentHeader)))
        header.uid = self.uid
        header.size = self.size
        header.fragment_index = self.fragment_index
        header.total_fragments = self.total_fragments
        return header


@dataclass(frozen=True)
class SensorPacket:
    fragments: tuple[SensorFragment, ...]

    @classmethod
    def from_bytes(cls, data: bytes) -> "SensorPacket":
        raw = bytes(data)
        if len(raw) < 4:
            raise ValueError("Sensor packet too short")
        if int.from_bytes(raw[:4], byteorder="little") != SENSOR_PACKET_TYPE_HEADER:
            raise ValueError("Invalid sensor packet type header")

        fragments = []
        offset = 4
        header_size = ctypes.sizeof(SensorFragmentHeader)
        while offset + header_size <= len(raw):
            header = SensorFragmentHeader(raw[offset : offset + header_size])
            offset += header_size
            if header.uid == SENSOR_PACKET_TERMINATOR_UID:
                packet = cls(tuple(fragments))
                packet._validate_single_sensor()
                return packet

            if header.total_fragments == 0 or header.fragment_index >= header.total_fragments:
                raise ValueError(f"Invalid sensor fragment index {header.fragment_index}/{header.total_fragments}")

            end = offset + header.size
            if end > len(raw):
                raise ValueError("Sensor fragment exceeds packet boundary")

            fragments.append(
                SensorFragment(
                    uid=int(header.uid),
                    fragment_index=int(header.fragment_index),
                    total_fragments=int(header.total_fragments),
                    payload=raw[offset:end],
                )
            )
            offset = end

        raise ValueError("Sensor packet terminator missing")

    @property
    def antenna_id(self) -> int | None:
        return self.fragments[0].antenna_id if self.fragments else None

    def _validate_single_sensor(self):
        antenna_ids = {fragment.antenna_id for fragment in self.fragments}
        if len(antenna_ids) > 1:
            raise ValueError("Sensor packet contains fragments from multiple sensors")


_PayloadT = TypeVar("_PayloadT")
_DecodedT = TypeVar("_DecodedT")


@dataclass(frozen=True)
class SensorMessage(Generic[_PayloadT]):
    uid: int
    antenna_id: int
    payload: _PayloadT


class SensorMessageReassembler:
    """Reassemble complete sensor messages from fragments, keyed by UID."""

    def __init__(self, timeout_s: float = 5.0, logger=None):
        self.timeout_s = timeout_s
        self._logger = logger
        self._entries = {}

    def clear(self):
        self._entries.clear()

    def _drop_stale(self, now: float):
        stale_uids = [uid for uid, entry in self._entries.items() if now - entry["timestamp"] > self.timeout_s]
        for uid in stale_uids:
            self._entries.pop(uid, None)

    def push(
        self,
        packet_or_fragments: SensorPacket | Iterable[SensorFragment],
        now: float | None = None,
    ) -> list[SensorMessage[bytes]]:
        if now is None:
            now = time.monotonic()

        self._drop_stale(now)
        fragments = packet_or_fragments.fragments if isinstance(packet_or_fragments, SensorPacket) else packet_or_fragments
        completed_messages = []

        for fragment in fragments:
            uid = int(fragment.uid)
            if fragment.total_fragments <= 0 or fragment.fragment_index >= fragment.total_fragments:
                if self._logger is not None:
                    self._logger.debug(f"Ignoring invalid sensor fragment index " f"{fragment.fragment_index}/{fragment.total_fragments} for uid {uid}")
                self._entries.pop(uid, None)
                continue

            entry = self._entries.get(uid)
            if entry is None or entry["total_fragments"] != fragment.total_fragments:
                entry = {
                    "timestamp": now,
                    "antenna_id": fragment.antenna_id,
                    "total_fragments": fragment.total_fragments,
                    "parts": {},
                }
                self._entries[uid] = entry

            entry["timestamp"] = now
            entry["parts"][fragment.fragment_index] = bytes(fragment.payload)

            if len(entry["parts"]) != entry["total_fragments"]:
                continue
            if any(index not in entry["parts"] for index in range(entry["total_fragments"])):
                continue

            completed_messages.append(
                SensorMessage(
                    uid=uid,
                    antenna_id=entry["antenna_id"],
                    payload=b"".join(entry["parts"][index] for index in range(entry["total_fragments"])),
                )
            )
            self._entries.pop(uid, None)

        return completed_messages


def get_sensor_message_type_header(message: SensorMessage[bytes]) -> int:
    """Return the four-byte logical type header of a reassembled message."""
    if len(message.payload) < 4:
        raise ValueError("Sensor message too short")
    return int.from_bytes(message.payload[:4], byteorder="little")


def decode_sensor_message(
    message: SensorMessage[bytes],
    decoder: Callable[[bytes], _DecodedT],
) -> SensorMessage[_DecodedT]:
    """Decode a reassembled message while preserving its transport metadata."""

    return replace(message, payload=decoder(message.payload))
