"""TX–RX timing from a known coupling path, after relative array calibration."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

import numpy as np

from .calibration import Antenna, Calibration


@dataclass(frozen=True)
class TxRxDelayCalibration:
    """Per-transmitter hardware delay relative to one coherent receiver group.

    Measured by ``Pool.calibrate_tx_rx_delay()`` using its own probes.
    ``calibration`` records the RX/TX corrections used during measurement.
    The result corrects timing but leaves the common TX–RX phase unknown.
    """

    calibration: Calibration
    delays_s: Mapping[Antenna, float]

    def for_calibration(self, calibration):
        """Keep range zero after retiming the same measured RX/TX groups.

        Use the calibration returned by the pool after a mode switch.
        The original RX/TX measurements must still cover these antennas.
        """
        groups = {}
        for g in calibration.groups:
            groups.setdefault(g.reference_id, []).extend(g.corrections)
        retained = []
        for group in self.calibration.groups:
            current = groups.get(group.reference_id)
            if current is None:
                raise ValueError("TX–RX timing requires the original calibration groups")
            states = {c.state.antenna: c.state for c in current}
            for c in group.corrections:
                old = c.state
                new = states.get(old.antenna)
                if new is None or (old.direction, old.frequency_hz, old.sample_rate_hz, old.boot_id, old.settings) != (new.direction, new.frequency_hz, new.sample_rate_hz, new.boot_id, new.settings):
                    raise ValueError("TX–RX timing no longer covers these sensors")
            selected = {c.state.antenna for c in group.corrections}
            retained.append(replace(group, corrections=tuple(c for c in current if c.state.antenna in selected)))
        return replace(self, calibration=Calibration(tuple(retained), calibration.diagnostics))

    def __post_init__(self):
        delays = dict(self.delays_s)
        if not all(math.isfinite(v) for v in delays.values()):
            raise ValueError("TX–RX delays must be finite")
        transmitters = {c.state.antenna for g in self.calibration.select(direction="tx").groups for c in g.corrections}
        if set(delays) != transmitters:
            raise ValueError("Provide a delay for each calibrated transmitter")
        object.__setattr__(self, "delays_s", MappingProxyType(delays))
        groups = self.calibration.groups
        if sorted(g.corrections[0].state.direction for g in groups) != ["rx", "tx"]:
            raise ValueError("Select one coherent RX group and one coherent TX group")
        if len({c.state.frequency_hz for g in groups for c in g.corrections}) != 1:
            raise ValueError("TX–RX timing requires one carrier frequency")

    @classmethod
    def from_reference(cls, calibration, *, measured_delay_s, reference_delay_s=0.0):
        """Store hardware delay from a measured path with known propagation time.

        Apply RX/TX corrections before measuring and resolve any delay ambiguity.
        Select the groups used for the measurement. ``reference_delay_s`` is
        the known propagation time through the cable or antenna path.
        """
        if not math.isfinite(measured_delay_s) or not math.isfinite(reference_delay_s) or reference_delay_s < 0:
            raise ValueError("Reference delays must be finite, with nonnegative propagation time")
        transmitters = {c.state.antenna for g in calibration.select(direction="tx").groups for c in g.corrections}
        return cls(calibration, {a: float(measured_delay_s - reference_delay_s) for a in transmitters})

    def response(self, transmitter, frequencies_hz):
        """Remove hardware delay at absolute RF frequencies, without shifting carrier phase."""
        center = self.calibration.groups[0].corrections[0].state.frequency_hz
        offsets = np.asarray(frequencies_hz) - center
        return np.exp(2j * np.pi * offsets * self.delays_s[transmitter])
