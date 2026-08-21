"""Decide whether CSI observations from different sensors saw the same frame.

The model deliberately separates two questions:

* ``FrameSignature`` — **What does the observed frame look like?**

  It contains metadata that must agree exactly: MAC addresses, sequence number,
  frame control, channel, PHY format, and related fields. Equal signatures are
  necessary for two observations to belong together, but are not proof that
  they came from the same transmission. ACKs have little identifying metadata,
  and broken transmitters may reuse the same sequence number indefinitely.

* ``FrameIdentity`` — **Which particular transmission could this be?**

  It wraps the signature with the association policy for one cluster candidate.
  With a common REFTX calibration, it also contains a calibrated timestamp and
  requires a timestamp match. Its ``instance_id`` merely gives a newly created
  cluster a unique dictionary key; it is not compared when observations match.

An observation matches a cluster's first observation only when:

1. their signatures are exactly equal;
2. they use the same timestamp policy; and
3. when timestamps are required, they belong to the same calibration epoch and
   differ by no more than 1 us.

Without usable timestamp association, ordinary data frames fall back to
signature-only matching. Control frames do not, because their signatures are
not sufficiently unique, so they are dropped instead.

The approximate 1 us comparison lives in :meth:`FrameIdentity.match`, not in
Python equality. "Within 1 us" is not transitive and therefore cannot safely
define hashing or ``__eq__``.
"""

from dataclasses import dataclass
import math

from . import csi_packet
from . import wifi

__all__ = [
    "CONTROL_FRAME_TYPE",
    "FRAME_TIMESTAMP_TOLERANCE_NS",
    "FrameIdentity",
    "FrameIdentityMatch",
    "FrameSignature",
    "frame_reference_timestamp_ns",
    "is_control_frame",
]

CONTROL_FRAME_TYPE = 1
FRAME_TIMESTAMP_TOLERANCE_NS = 1_000


def is_control_frame(packet: csi_packet.CSIPacket) -> bool:
    """Return whether ``packet`` is an IEEE 802.11 control frame."""

    return int(packet.frame_ctrl.type) == CONTROL_FRAME_TYPE


def _secondary_channel_relative(rx_ctrl: csi_packet.WiFiPacketRxControlV3) -> int:
    if rx_ctrl.cur_bb_format in (
        csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_11B,
        csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_11G,
    ):
        return 0
    if rx_ctrl.second == 1:
        return 1
    if rx_ctrl.second == 2:
        return -1
    return 0


@dataclass(frozen=True)
class FrameSignature:
    """Observable frame metadata that must match exactly.

    A signature is a necessary match condition, not a unique transmission ID:
    distinct frames may have identical signatures.
    """

    frame_key: wifi.WiFiFrameKey
    frame_control: int
    channel: int
    secondary_channel_relative: int
    baseband_format: int
    signal_mode: int
    rate: int
    channel_estimate_length: int
    is_calibration: bool
    is_radar: bool

    @classmethod
    def from_packet(cls, packet: csi_packet.CSIPacket) -> "FrameSignature":
        rx_ctrl = csi_packet.WiFiPacketRxControlV3(packet.rx_ctrl)
        return cls(
            frame_key=wifi.WiFiFrameKey.from_packet(packet),
            frame_control=int.from_bytes(bytes(packet.frame_ctrl), byteorder="little"),
            channel=int(rx_ctrl.channel),
            secondary_channel_relative=_secondary_channel_relative(rx_ctrl),
            baseband_format=int(rx_ctrl.cur_bb_format),
            signal_mode=int(rx_ctrl.sig_mode),
            rate=int(rx_ctrl.rate),
            channel_estimate_length=int(rx_ctrl.rx_channel_estimate_len),
            is_calibration=bool(packet.is_calibration),
            is_radar=bool(packet.is_radar),
        )


@dataclass(frozen=True)
class FrameIdentityMatch:
    """Successful comparison of two frame identities."""

    timestamp_residual_ns: int | None


@dataclass(frozen=True)
class FrameIdentity:
    """One cluster candidate: a signature plus its timestamp-matching policy.

    ``instance_id`` makes newly created cluster keys unique; :meth:`match` does
    not compare it. Instead, identities match through their signatures and,
    when ``timestamp_required`` is true, their calibrated timestamps and
    calibration epochs.
    """

    instance_id: int
    signature: FrameSignature
    timestamp_ns: int | None = None
    timestamp_required: bool = False
    calibration_epoch: int | None = None

    def __post_init__(self):
        if self.timestamp_required and (self.timestamp_ns is None or self.calibration_epoch is None):
            raise ValueError("timestamp-required identities need a timestamp and calibration epoch")
        if not self.timestamp_required and (self.timestamp_ns is not None or self.calibration_epoch is not None):
            raise ValueError("metadata-only identities cannot carry calibrated timestamp data")

    @property
    def frame_key(self) -> wifi.WiFiFrameKey:
        """Return the conventional Wi-Fi metadata exposed by ``CSICluster``."""

        return self.signature.frame_key

    def match(self, other: "FrameIdentity") -> FrameIdentityMatch | None:
        """Compare two identities using exact metadata and, when required, time."""

        if self.signature != other.signature:
            return None
        if self.timestamp_required != other.timestamp_required:
            return None
        if not self.timestamp_required:
            return FrameIdentityMatch(timestamp_residual_ns=None)
        if self.calibration_epoch != other.calibration_epoch:
            return None

        residual_ns = abs(self.timestamp_ns - other.timestamp_ns)
        if residual_ns > FRAME_TIMESTAMP_TOLERANCE_NS:
            return None
        return FrameIdentityMatch(timestamp_residual_ns=residual_ns)


def frame_reference_timestamp_ns(
    packet: csi_packet.CSIPacket,
    clock_offset_s: float,
) -> int | None:
    """Return the hardware timestamp in the REFTX reference clock domain.

    Association deliberately uses no CSI-derived value. The packet's precise
    hardware timestamp is corrected only by the per-sensor clock offset from a
    prior REFTX calibration.
    """

    try:
        clock_offset_s = float(clock_offset_s)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(clock_offset_s):
        return None

    try:
        sample_time_ns = packet.get_hardware_rx_timestamp_ns()
    except (AttributeError, TypeError, ValueError):
        return None
    return int(round(sample_time_ns - clock_offset_s * 1e9))
