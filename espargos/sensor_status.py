"""In-band sensor snapshots. An omitted section retains its own last-update time."""

from dataclasses import dataclass
import struct
import zlib

MAGIC = 0x31535453  # STS1
HEADER = struct.Struct("<7I")
TX = 6
STEERING = 7
BLE = 9
WIFI_IQ = 10
_SECTION_SIZES = {2: 4, 3: 112, 5: 128, TX: 112, STEERING: 40, BLE: 76, WIFI_IQ: 56}


@dataclass(frozen=True)
class SensorStatus:
    antenna: int
    boot_id: int
    sequence: int
    mode: int
    rx_generation: int
    stream_session: int
    sections: dict


def decode(message):
    data = memoryview(message.payload)
    if len(data) < HEADER.size + 4:
        return None
    magic, antenna, boot, seq, mode, generation, session = HEADER.unpack_from(data)
    end = len(data) - 4
    if (magic != MAGIC or antenna >= 8 or antenna != message.antenna_id or not boot or
            zlib.crc32(data[:end]) != struct.unpack_from("<I", data, end)[0]):
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
        if size != _SECTION_SIZES.get(tag, size):
            return None
        if tag == 4 and (size < 4 or data[at + 1] > 80 or size != 4 + 12 * data[at + 1]):
            return None
        if tag == TX and (struct.unpack_from("<I", data, at + 28)[0] != boot or
                          struct.unpack_from("<I", data, at + 52)[0] != mode):
            return None
        sections[tag] = data[at:at + size]
        at += size
    return SensorStatus(antenna, boot, seq, mode, generation, session, sections)


def playback_state(report):
    payload = report.sections.get(TX)
    if payload is None:
        return None
    v = struct.unpack("<9I3f16I", payload)
    fields = ("generation", "samples", "crc32", "error", "enabled", "clipped_samples",
              "update_us", "boot_id", "sequence", "phase_deg", "delay_samples", "applied_amplitude")
    state = dict(zip(fields, v))
    state.update(report_sequence=report.sequence, applied_mode=mode_name(report.mode),
                 epoch=v[15], sync_seq=v[16], prepared_generation=v[17], applied_generation=v[18],
                 sample_position=v[19] | v[20] << 32, applied_sample=v[23] | v[24] << 32,
                 origin_ns=v[25] | v[26] << 32, sample_rate_hz=v[27], applied_sample_rate_hz=v[27])
    return state


def steering_state(report):
    payload = report.sections.get(STEERING)
    if payload is None:
        return None
    fields = ("steer_id", "steer_received", "steer_applied", "steer_free", "steer_late",
              "steer_full", "steer_missing", "steer_rejected", "steer_error", "steer_cancelled")
    return dict(zip(fields, struct.unpack("<10I", payload)))


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
