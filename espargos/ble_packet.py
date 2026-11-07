"""BLE packet-IQ records and advertiser identity from decoded PDUs."""

from __future__ import annotations

import struct
import numpy as np

BLE_PACKET_TYPE_HEADER = 0x35514C42  # BLQ5
ADVERTISING_ACCESS_ADDRESS = 0x8E89BED6


def channel_frequency(channel):
    if not 0 <= channel <= 39:
        raise ValueError("BLE channel must be in 0..39")
    return (
        2402
        if channel == 37
        else 2426 if channel == 38 else 2480 if channel == 39 else 2404 + 2 * channel if channel < 11 else 2406 + 2 * channel
    ) * 1_000_000


class BLEPacket:
    """One BLQ5 capture, with packet qualification and CRC verification separated.

    The retained PDU prefix is at most 32 bytes, so advertising data and names
    can be incomplete. IQ samples keep the native tap's I+jQ convention here.
    """

    def __init__(self, payload):
        self._data = bytes(payload)
        data = memoryview(self._data)
        if len(data) < 128:
            raise ValueError("BLE record is shorter than its header")
        h = struct.unpack_from("<18I", data)
        if h[0] != BLE_PACKET_TYPE_HEADER:
            raise ValueError("Expected a BLQ5 record")
        self.sequence = h[1]
        self.time_us = h[6] | h[7] << 32
        self.clock_epoch, self.receiver_epoch, self.settings, self.flags = h[8:12]
        self.channel = self.settings & 255
        self.rf_switch_mode = (self.settings >> 8) & 255
        self.rf_switch = self.rf_switch_mode
        self.generation = h[16]
        self.dropped = h[17]
        words, divider = h[14], h[13]
        packed = bool(self.flags & 4)
        if divider not in (1, 2, 4, 8) or not 128 <= words <= 16384 or words % 8 or h[15] != words:
            raise ValueError("Invalid BLE capture size or sample divider")
        if len(data) != 128 + words * (5 if packed else 8) // 2:
            raise ValueError("BLE payload size mismatch")
        self.sample_rate, self.if_hz, self.access_address, self.packet_crc, self.packet_flags = struct.unpack_from("<Ii3I", data, 72)
        self.phy, length, actual_rf_switch, reserved = struct.unpack_from("<4B", data, 92)
        if reserved or self.packet_crc > 0xffffff:
            raise ValueError("Invalid BLE packet metadata")
        if self.rf_switch_mode == 4:
            if actual_rf_switch not in (2, 3):
                raise ValueError("Random polarization capture must report its effective antenna input")
            self.rf_switch = actual_rf_switch
        if self.sample_rate != 32_000_000 // divider or self.phy > 1 or length > 32 or self.channel > 39:
            raise ValueError("Invalid BLE modem metadata")
        self.gain_reported = bool(self.packet_flags & 8)
        self.rx_gain = (self.packet_flags >> 8) & 255 if self.packet_flags & 4 else None
        if self.rx_gain is not None and self.rx_gain > 76:
            raise ValueError("Invalid measured BLE gain")
        if self.rx_gain is None and self.settings >> 24 == 0:
            self.rx_gain = (self.settings >> 16) & 255
        self.capture_phy = self.phy
        self.coding = 0
        self.prefix = bytes(data[96 : 96 + length])
        if packed:
            b = np.frombuffer(data[128:], dtype=np.uint8).reshape(-1, 5).astype(np.uint32)
            raw = np.empty(words, np.uint32)
            raw[::2] = b[:, 0] | b[:, 1] << 8 | (b[:, 2] & 15) << 16
            raw[1::2] = b[:, 2] >> 4 | b[:, 3] << 4 | b[:, 4] << 12
        else:
            raw = np.frombuffer(data[128:], dtype="<u4")
        if self.raw_capture:
            # Raw records cannot supply decoded identity or CRC verdicts.
            self.access_address = self.packet_crc = 0
            self.prefix = b""
            self.packet_flags &= ~(1 | 2 | 32)
            self._decode_on_host(raw)
        i = (raw & 1023).astype(np.int32)
        q = ((raw >> 10) & 1023).astype(np.int32)
        self.samples = (np.where(i >= 512, i - 1024, i) + 1j * np.where(q >= 512, q - 1024, q)).astype(np.complex64)

    def _decode_on_host(self, raw):
        # Original wire bytes remain unchanged, including local CRC provenance.
        from .ble_decoder import decode
        if self.if_hz != 2_000_000:
            return
        channel = 37 if self.channel < 37 and self.rf_switch == 1 and self.phy == 0 else self.channel
        decoded = decode(np.ascontiguousarray(raw, dtype=np.uint32), 32_000_000 // self.sample_rate, channel, self.phy)
        if decoded is None:
            return
        self.access_address = decoded.access_address
        self.packet_crc = decoded.packet_crc
        self.packet_flags |= decoded.flags | 32
        self.prefix = decoded.prefix
        self.coding = decoded.coding
        self.phy = decoded.phy

    @property
    def host_decoded(self):
        """Whether the host successfully decoded this observation's IQ."""
        return bool(self.packet_flags & 32)

    def __bytes__(self):
        return self._data

    @property
    def raw_capture(self):
        """True for raw candidate IQ that needs host decoding."""
        return bool(self.packet_flags & 16)

    @property
    def qualified(self):
        return bool(self.packet_flags & 1) and len(self.prefix) >= 2

    @property
    def crc_valid(self):
        return self.packet_flags & 3 == 3 and len(self.prefix) >= 2

    @property
    def synchronized(self):
        return self.flags & 3 == 3

    @property
    def identity(self):
        return (self.access_address, self.phy, self.prefix, self.packet_crc) if self.qualified else None

    @property
    def advertiser(self):
        if not self.crc_valid or self.access_address != ADVERTISING_ACCESS_ADDRESS:
            return None
        p = self.prefix
        kind = p[0] & 15
        # SCAN_REQ and CONNECT_IND put the scanner/initiator first, AdvA second.
        if kind in (3, 5):
            address = p[8:14] if len(p) >= 14 else None
        elif kind == 7:
            address = p[4:10] if len(p) >= 10 and (p[2] & 63) >= 7 and p[3] & 1 else None
        elif kind in (0, 1, 2, 4, 6):
            address = p[2:8] if len(p) >= 8 else None
        else:
            address = None
        return ":".join(f"{b:02x}" for b in reversed(address)) if address else None

    @property
    def is_advertisement(self):
        # Requests from a scanner name the advertiser but are transmitted by
        # someone else. Never attribute their direction to that advertiser.
        return self.advertiser is not None and self.prefix[0] & 15 in (0, 1, 2, 4, 6, 7)

    @property
    def address_type(self):
        """Type of the advertiser address, including RxAdd in scanner requests."""
        mask = 128 if self.prefix and self.prefix[0] & 15 in (3, 5) else 64
        return "random" if self.prefix and self.prefix[0] & mask else "public"

    @property
    def name(self):
        if not self.is_advertisement or self.prefix[0] & 15 not in (0, 2, 4, 6):
            return ""
        data = self.prefix[8 : min(len(self.prefix), 2 + self.prefix[1])]
        offset = 0
        while offset < len(data):
            size = data[offset]
            if not size or offset + size + 1 > len(data):
                break
            if size >= 2 and data[offset + 1] in (8, 9):
                return data[offset + 2 : offset + size + 1].decode("utf-8", errors="replace")
            offset += size + 1
        return ""
