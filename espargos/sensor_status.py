"""In-band sensor snapshots. An omitted section retains its own last-update time."""

from dataclasses import dataclass
import struct
import zlib

MAGIC = 0x32535453  # STS2
HEADER = struct.Struct("<4I")
MODE, RX_GENERATION, STREAM_SESSION = 1, 7, 8
_PLAYBACK = {
    26: (struct.Struct("<I"), ("generation",)),
    27: (struct.Struct("<II"), ("samples", "crc32")),
    28: (struct.Struct("<I"), ("error",)),
    29: (struct.Struct("<I"), ("enabled",)),
    30: (struct.Struct("<II"), ("clipped_samples", "update_us")),
    31: (struct.Struct("<I"), ("sequence",)),
    32: (struct.Struct("<fff"), ("phase_deg", "delay_samples", "applied_amplitude")),
    33: (struct.Struct("<I"), ("rf_gain",)),
    34: (struct.Struct("<I"), ("temporary_active",)),
    35: (struct.Struct("<II"), ("epoch", "sync_seq")),
    36: (struct.Struct("<II"), ("prepared_generation", "applied_generation")),
    37: (struct.Struct("<Q"), ("sample_position",)),
    38: (struct.Struct("<Q"), ("start_sample",)),
    39: (struct.Struct("<Q"), ("applied_sample",)),
    40: (struct.Struct("<Q"), ("origin_ns",)),
    41: (struct.Struct("<I"), ("applied_sample_rate_hz",)),
}
_STEERING = {
    42: (struct.Struct("<IIII"), ("steer_id", "steer_received", "steer_applied", "steer_free")),
    43: (struct.Struct("<IIII"), ("steer_late", "steer_full", "steer_missing", "steer_rejected")),
    44: (struct.Struct("<I"), ("steer_error",)),
    45: (struct.Struct("<I"), ("steer_cancelled",)),
}
_PREFIX_BYTES = {
    1: 4,
    2: 4,
    3: 32,
    4: 4,
    5: 48,
    6: 32,
    7: 4,
    8: 4,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
    13: 12,
    14: 4,
    15: 12,
    16: 12,
    17: 12,
    18: 4,
    19: 4,
    20: 8,
    21: 4,
    22: 4,
    23: 8,
    24: 8,
    25: 8,
    26: 4,
    27: 8,
    28: 4,
    29: 4,
    30: 8,
    31: 4,
    32: 12,
    33: 4,
    34: 4,
    35: 8,
    36: 8,
    37: 8,
    38: 8,
    39: 8,
    40: 8,
    41: 4,
    42: 16,
    43: 16,
    44: 4,
    45: 4,
    46: 16,
    47: 12,
    48: 8,
    49: 4,
    50: 12,
    51: 16,
    52: 4,
    53: 4,
    54: 16,
    55: 12,
    56: 12,
    57: 8,
    58: 8,
}


@dataclass(frozen=True)
class SensorStatus:
    antenna: int
    boot_id: int
    sequence: int
    mode: int | None
    rx_generation: int | None
    stream_session: int | None
    sections: dict


def decode(message):
    data = memoryview(message.payload)
    if len(data) < HEADER.size + 4:
        return None
    magic, antenna, boot, seq = HEADER.unpack_from(data)
    end = len(data) - 4
    if magic != MAGIC or antenna >= 8 or antenna != message.antenna_id or not boot or zlib.crc32(data[:end]) != struct.unpack_from("<I", data, end)[0]:
        return None
    at = HEADER.size
    sections = {}
    while at < end:
        if at + 4 > end:
            return None
        tag, size = struct.unpack_from("<HH", data, at)
        at += 4
        if at + size > end or size % 4 or tag in sections:
            return None
        if size < _PREFIX_BYTES.get(tag, 0):
            return None
        if tag == 4 and (size < 4 or data[at + 1] > 80 or size != 4 + 12 * data[at + 1]):
            return None
        sections[tag] = data[at : at + size]
        at += size

    def word(tag):
        return struct.unpack_from("<I", sections[tag])[0] if tag in sections else None

    return SensorStatus(antenna, boot, seq, word(MODE), word(RX_GENERATION), word(STREAM_SESSION), sections)


def _read_fields(report, layout):
    fields = {}
    for tag, payload in report.sections.items():
        if tag in layout:
            fmt, names = layout[tag]
            fields.update(zip(names, fmt.unpack_from(payload)))
    return fields


def playback_state(report):
    state = _read_fields(report, _PLAYBACK)
    if "applied_sample_rate_hz" in state:
        state["sample_rate_hz"] = state["applied_sample_rate_hz"]
    return state


def steering_state(report):
    return _read_fields(report, _STEERING)


def mode_name(mode):
    return {0: "wifi", 1: "iq_rx", 2: "iq_tx", 3: "ble", 4: "wifi_iq", 255: "transitioning"}.get(mode)


class ReportClock:
    """Latest accepted report per sensor, including the sensor's stream session."""

    def __init__(self):
        self.latest = {}
        self._retired = {}

    def accept(self, key, report):
        previous = self.latest.get(key)
        if previous:
            if report.boot_id == previous.boot_id:
                if not 0 < (report.sequence - previous.sequence) % (1 << 32) < (1 << 31):
                    return False
            elif report.boot_id == self._retired.get(key):
                return False
            else:
                self._retired[key] = previous.boot_id
        self.latest[key] = report
        return True
