"""Phase/delay corrections and the hardware states for which they are valid.

Groups have a common phase reference. Combining groups stores independent
measurements; it does not establish coherence between their references.
"""

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from collections.abc import Mapping
import math
from numbers import Integral
import uuid
import numpy as np


@dataclass(frozen=True, order=True)
class Antenna:
    """A controller address and its hardware antenna ID (not row/column)."""

    host: str
    antid: int

    def __post_init__(self):
        if not self.host or not isinstance(self.antid, Integral) or not 0 <= self.antid < 8:
            raise ValueError("An antenna needs a controller address and ID 0–7")


@dataclass(frozen=True)
class CalibrationState:
    """Phase-sensitive state; gain and waveform amplitude are deliberately absent.

    ``epoch`` is RX fire_time_ns or TX origin_ns. Reboot IDs distinguish a
    restarted device even when its local timestamp happens to repeat.
    ``settings`` can bind additional application-specific configuration.
    """

    antenna: Antenna
    direction: str
    frequency_hz: float
    sample_rate_hz: float
    boot_id: int
    epoch: int
    settings: tuple = ()

    def __post_init__(self):
        if self.direction not in ("rx", "tx"):
            raise ValueError("direction must be rx or tx")
        if not all(math.isfinite(v) and v > 0 for v in (self.frequency_hz, self.sample_rate_hz)):
            raise ValueError("frequency and sample rate must be positive and finite")
        if not isinstance(self.boot_id, Integral) or not isinstance(self.epoch, Integral) or self.boot_id < 0 or self.epoch < 0:
            raise ValueError("boot ID and epoch must be nonnegative")
        hash(self.settings)

    @classmethod
    def from_tx_status(cls, host, sensor):
        if sensor.get("applied_mode") != "iq_tx" or not sensor.get("enabled") or not sensor.get("origin_ns"):
            raise ValueError("Sensor is not running timestamped IQ TX")
        return cls(Antenna(host, sensor["antid"]), "tx", sensor["frequency_hz"], 80e6, sensor["boot_id"], sensor["origin_ns"], (("samples", sensor["samples"]),))

    @classmethod
    def from_iq_packet(cls, host, antid, packet, boot_id):
        """Validate RX against an actual packet and the device's current boot ID."""
        return cls(Antenna(host, antid), "rx", packet.center_freq_hz, packet.sample_rate_hz, boot_id, packet.fire_time_ns)


@dataclass(frozen=True)
class AntennaCorrection:
    """Multiply by exp(j*phase_rad - j*2π*(f-center)*delay_s)."""

    state: CalibrationState
    phase_rad: float
    delay_s: float

    def __post_init__(self):
        if not math.isfinite(self.phase_rad) or not math.isfinite(self.delay_s):
            raise ValueError("Corrections must be finite")

    def response(self, frequencies_hz):
        return np.exp(1j * self.phase_rad - 2j * np.pi * (np.asarray(frequencies_hz) - self.state.frequency_hz) * self.delay_s)


@dataclass(frozen=True)
class CalibrationGroup:
    """One coherent group, for one direction, frequency and sample rate.

    ``reference_id`` identifies the measurement's phase reference. A subset
    retains this ID, even when it no longer contains the reference antenna.
    """

    corrections: tuple[AntennaCorrection, ...]
    reference_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self):
        object.__setattr__(self, "corrections", tuple(self.corrections))
        states = [c.state for c in self.corrections]
        if not states or len({s.antenna for s in states}) != len(states):
            raise ValueError("A group needs distinct antennas")
        if len({(s.direction, s.frequency_hz, s.sample_rate_hz) for s in states}) != 1:
            raise ValueError("A group must share direction, frequency and sample rate")


@dataclass(frozen=True)
class Calibration:
    """An immutable collection of scoped RX and/or TX calibration groups.

    Looking up an unmeasured or stale state raises KeyError, never silently
    substitutes zero correction. Different frequencies and subsets can coexist.
    """

    groups: tuple[CalibrationGroup, ...] = ()
    diagnostics: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "groups", tuple(self.groups))
        object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))
        states = [c.state for g in self.groups for c in g.corrections]
        if len(set(states)) != len(states):
            raise ValueError("Ambiguous calibration: duplicate antenna states")

    def correction(self, state):
        for group in self.groups:
            for correction in group.corrections:
                if correction.state == state:
                    return correction
        raise KeyError(f"No calibration for {state}")

    def covers(self, states):
        try:
            for state in states:
                self.correction(state)
            return True
        except KeyError:
            return False

    def select(self, antennas=None, direction=None, frequency_hz=None):
        antennas = None if antennas is None else set(antennas)
        groups = []
        for group in self.groups:
            selected = tuple(c for c in group.corrections if (antennas is None or c.state.antenna in antennas) and (direction is None or c.state.direction == direction) and (frequency_hz is None or c.state.frequency_hz == frequency_hz))
            if selected:
                groups.append(replace(group, corrections=selected))
        return Calibration(tuple(groups), self.diagnostics)

    def merge(self, newer):
        """Replace overlapping antenna/direction/frequency measurements.

        Other frequency groups remain stored, but only match their original
        hardware epochs. Retuning back does not make an old calibration valid.
        """

        def key(c):
            s = c.state
            return s.antenna, s.direction, s.frequency_hz

        replaced = {key(c) for g in newer.groups for c in g.corrections}
        groups = []
        for group in self.groups:
            keep = tuple(c for c in group.corrections if key(c) not in replaced)
            if keep:
                groups.append(replace(group, corrections=keep))
        return Calibration(tuple(groups) + newer.groups, newer.diagnostics)
