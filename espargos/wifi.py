"""Shared Wi-Fi protocol types.

These definitions are used by more than one sensor-message format. Keeping
them here avoids making radar packets depend on CSI packet implementation
details.
"""

import ctypes
from dataclasses import dataclass
from enum import IntEnum


@dataclass(frozen=True)
class WiFiFrameKey:
    """Reusable 802.11 fields used to associate observations of a frame.

    This is not a globally unique physical-transmission identifier: sequence
    numbers wrap after 4096 values and retransmissions reuse them.
    """

    source_mac: bytes
    destination_mac: bytes
    sequence_number: int
    fragment_number: int

    @classmethod
    def from_packet(cls, packet) -> "WiFiFrameKey":
        return cls(
            source_mac=bytes(packet.source_mac),
            destination_mac=bytes(packet.dest_mac),
            sequence_number=int(packet.seq_ctrl.seg),
            fragment_number=int(packet.seq_ctrl.frag),
        )


class WiFiTxPower(IntEnum):
    WIFI_TX_POWER_2_DBM = 8
    WIFI_TX_POWER_5_DBM = 20
    WIFI_TX_POWER_7_DBM = 28
    WIFI_TX_POWER_8_5_DBM = 34
    WIFI_TX_POWER_11_DBM = 44
    WIFI_TX_POWER_13_DBM = 52
    WIFI_TX_POWER_14_DBM = 56
    WIFI_TX_POWER_15_DBM = 60
    WIFI_TX_POWER_16_5_DBM = 66
    WIFI_TX_POWER_18_DBM = 72
    WIFI_TX_POWER_20_DBM = 80


class WiFiPhyMode(IntEnum):
    WIFI_PHY_MODE_LR = 0
    WIFI_PHY_MODE_11B = 1
    WIFI_PHY_MODE_11G = 2
    WIFI_PHY_MODE_11A = 3
    WIFI_PHY_MODE_HT20 = 4
    WIFI_PHY_MODE_HT40 = 5
    WIFI_PHY_MODE_HE20 = 6
    WIFI_PHY_MODE_VHT20 = 7


class WiFiPhyRate(IntEnum):
    WIFI_PHY_RATE_1M_L = 0x00
    WIFI_PHY_RATE_2M_L = 0x01
    WIFI_PHY_RATE_5M_L = 0x02
    WIFI_PHY_RATE_11M_L = 0x03
    WIFI_PHY_RATE_2M_S = 0x05
    WIFI_PHY_RATE_5M_S = 0x06
    WIFI_PHY_RATE_11M_S = 0x07
    WIFI_PHY_RATE_48M = 0x08
    WIFI_PHY_RATE_24M = 0x09
    WIFI_PHY_RATE_12M = 0x0A
    WIFI_PHY_RATE_6M = 0x0B
    WIFI_PHY_RATE_54M = 0x0C
    WIFI_PHY_RATE_36M = 0x0D
    WIFI_PHY_RATE_18M = 0x0E
    WIFI_PHY_RATE_9M = 0x0F
    WIFI_PHY_RATE_MCS0_LGI = 0x10
    WIFI_PHY_RATE_MCS1_LGI = 0x11
    WIFI_PHY_RATE_MCS2_LGI = 0x12
    WIFI_PHY_RATE_MCS3_LGI = 0x13
    WIFI_PHY_RATE_MCS4_LGI = 0x14
    WIFI_PHY_RATE_MCS5_LGI = 0x15
    WIFI_PHY_RATE_MCS6_LGI = 0x16
    WIFI_PHY_RATE_MCS7_LGI = 0x17
    WIFI_PHY_RATE_MCS8_LGI = 0x18
    WIFI_PHY_RATE_MCS9_LGI = 0x19
    WIFI_PHY_RATE_MCS0_SGI = 0x1A
    WIFI_PHY_RATE_MCS1_SGI = 0x1B
    WIFI_PHY_RATE_MCS2_SGI = 0x1C
    WIFI_PHY_RATE_MCS3_SGI = 0x1D
    WIFI_PHY_RATE_MCS4_SGI = 0x1E
    WIFI_PHY_RATE_MCS5_SGI = 0x1F
    WIFI_PHY_RATE_MCS6_SGI = 0x20
    WIFI_PHY_RATE_MCS7_SGI = 0x21
    WIFI_PHY_RATE_MCS8_SGI = 0x22
    WIFI_PHY_RATE_MCS9_SGI = 0x23
    WIFI_PHY_RATE_LORA_250K = 0x29
    WIFI_PHY_RATE_LORA_500K = 0x2A
    WIFI_PHY_RATE_MAX = 0x2B


class SequenceControl(ctypes.LittleEndianStructure):
    """The fragment and sequence numbers of an IEEE 802.11 frame."""

    _pack_ = 1
    _layout_ = "ms"
    _fields_ = [("frag", ctypes.c_uint16, 4), ("seg", ctypes.c_uint16, 12)]

    def __new__(cls, buf=None):
        return cls.from_buffer_copy(buf)

    def __init__(self, buf=None):
        pass
