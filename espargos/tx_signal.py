"""Signal assignments and spatial distributions for repeating IQ transmission."""

from dataclasses import dataclass, replace
from contextlib import contextmanager
import threading

import numpy as np

from .calibration import Antenna, CalibrationState
from .steering import Beamspace, Weights

from .waveforms import CW, IQSamples, Waveform, SAMPLE_RATE, _finite


@dataclass(frozen=True)
class _SignalSettings:
    waveform: Waveform
    amplitude: float
    antennas: tuple
    steering: object = None


class TXSignal:
    """An editable contribution to the selected antennas' replay buffers."""

    def __init__(self, collection, settings):
        self._collection, self._settings = collection, settings

    def update(self, **settings):
        with self._collection.transmitter.update():
            if self not in self._collection._items:
                raise ValueError("This signal has been removed")
            if "antennas" in settings:
                settings["antennas"] = self._collection._antennas(settings["antennas"])
            self._settings = replace(self._settings, **settings)

    def remove(self):
        with self._collection.transmitter.update():
            self._collection._items.remove(self)

    @property
    def antennas(self):
        return self._settings.antennas

    @property
    def waveform(self):
        return self._settings.waveform

    @waveform.setter
    def waveform(self, value):
        self.update(waveform=value)

    @property
    def amplitude(self):
        return self._settings.amplitude

    @amplitude.setter
    def amplitude(self, value):
        self.update(amplitude=value)

    @property
    def steering(self):
        return self._settings.steering

    @steering.setter
    def steering(self, value):
        self.update(steering=value)


class TXSignals:
    def __init__(self, transmitter):
        self.transmitter = transmitter
        self._items = []
        self._lock = threading.RLock()
        self._editing = False
        self._samples = 1024
        self._targets = set()

    def __iter__(self):
        return iter(tuple(self._items))

    def __len__(self):
        return len(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def _antennas(self, antennas):
        tx = self.transmitter
        available = {Antenna(b.host, a) for b in tx.boards for a in range(8)}
        if antennas is None:
            antennas = [Antenna(b.host, a) for i, b in enumerate(tx.boards) for a, mode in enumerate(tx.states[i]["modes"]) if mode == "iq_tx"]
        elif hasattr(antennas, "antennas"):
            antennas = antennas.antennas
        result = tuple(dict.fromkeys(antennas))
        if not result or not set(result) <= available:
            raise ValueError("Select antennas belonging to this pool")
        return result

    def add(self, *, waveform, amplitude=0.1, antennas=None, steering=None):
        with self.transmitter.update():
            signal = TXSignal(self, _SignalSettings(waveform, amplitude, self._antennas(antennas), steering))
            self._items.append(signal)
        return signal

    def clear(self):
        with self.transmitter.update():
            self._items.clear()

    @contextmanager
    def update(self, *, samples=None, start_sample=None):
        """Validate the complete configuration before sending any waveform commands."""
        tx = self.transmitter
        with self._lock:
            if tx.closed:
                raise RuntimeError("Transmitter is closed")
            if self._editing:
                if samples is not None or start_sample is not None:
                    raise ValueError("Set timing and replay length on the outer update")
                yield tx
                return
            old = [(signal, signal._settings) for signal in tx.signals]
            old_samples = self._samples
            self._editing = True
            try:
                if samples is not None:
                    if not isinstance(samples, int) or samples < 256 or samples > 8192 or samples & (samples - 1):
                        raise ValueError("Replay length must be a power of two from 256 to 8192")
                    self._samples = samples
                if start_sample is not None and (not isinstance(start_sample, int) or start_sample < 0 or start_sample >= 2**53 or start_sample % self._samples):
                    raise ValueError("start_sample must be a nonnegative replay-buffer boundary below 2**53")
                yield tx
                plans, targets = self._compile()
                tx._apply_signals(plans, start_sample)
                self._targets = targets
            except BaseException:
                self._items = [signal for signal, _ in old]
                for signal, settings in old:
                    signal._settings = settings
                self._samples = old_samples
                raise
            finally:
                self._editing = False

    def _check_beam_calibration(self, antennas=None, preparing=False):
        """Geometric beams need a common TX reference, raw weights do not."""
        tx = self.transmitter
        calibration = tx.owner.calibrations
        sensors = {Antenna(b.host, a): tx.states[bi]["sensors"][a] for bi, b in enumerate(tx.boards) for a in range(8)}
        known = {c.state.antenna for g in calibration.groups for c in g.corrections if c.state.direction == "tx"}
        for signal in self:
            if not isinstance(signal.steering, Beamspace):
                continue
            members = set(signal.antennas)
            if antennas is not None and not members.intersection(antennas):
                continue
            # Waveforms are normally prepared before the first calibration.
            if preparing and not members.intersection(known):
                continue
            states = [CalibrationState.from_tx_status(a.host, sensors[a]) for a in signal.antennas]
            calibration.require_coherent(states)

    def _compile(self):
        tx = self.transmitter
        self._check_beam_calibration(preparing=True)
        recipes, targets = {}, set()
        for signal in tx.signals:
            s = signal._settings
            if not isinstance(s.waveform, Waveform):
                raise TypeError("waveform must be a Waveform object")
            amplitude = _finite(s.amplitude, "amplitude")
            if not 0 <= amplitude <= 1:
                raise ValueError("amplitude must lie between 0 and 1")
            if s.steering is not None and not isinstance(s.steering, (Beamspace, Weights)):
                raise TypeError("steering must be Beamspace, Weights, or None")
            recipes[signal] = {**s.waveform.recipe(self._samples), "amplitude": 1.0}
            targets.update(s.antennas)
        plans = []
        for i, board in enumerate(tx.boards):
            groups = {}
            for a in range(8):
                antenna = Antenna(board.host, a)
                assigned = tuple(s for s in tx.signals if antenna in s.antennas)
                if not assigned and antenna not in self._targets:
                    continue
                if tx.states[i]["modes"][a] != "iq_tx":
                    if assigned:
                        raise ValueError(f"{antenna}: sensor must be in IQ TX mode")
                    continue
                if len(assigned) > 4:
                    raise ValueError(f"{antenna}: at most four signals can contribute to one sensor")
                groups.setdefault(assigned, []).append(a)
            board_plan = []
            for assigned, antennas in groups.items():
                records = []
                for slot, signal in enumerate(assigned):
                    s = signal._settings
                    weights, delays = np.zeros(8, complex), np.zeros(8)
                    for a in antennas:
                        antenna = Antenna(board.host, a)
                        if isinstance(s.steering, Beamspace):
                            position = None if tx.positions is None else tx.positions[i, a]
                            weights[a], delays[a] = s.steering.coefficients(position, tx.states[i]["sensors"][a]["frequency_hz"], s.waveform)
                        elif isinstance(s.steering, Weights):
                            weights[a] = s.steering.values.get(antenna, 1)
                            delays[a] = s.steering.delays_s.get(antenna, 0) * SAMPLE_RATE
                        else:
                            weights[a] = 1
                    weights *= s.amplitude
                    if np.any(abs(delays) > 8192):
                        raise ValueError("Additional delays must fit within ±8192 samples")
                    records.append((s.waveform, dict(waveform={**recipes[signal], "antennas": antennas}, real=weights.real.tolist(), imag=weights.imag.tolist(), delay_samples=delays.tolist())))
                if records:
                    total = sum(np.hypot(record["real"], record["imag"]) for _, record in records)
                    if np.any(total > 1.000001):
                        raise ValueError("Sum of signal amplitude × weight magnitude must not exceed 1 per sensor")
                if not records:
                    records = [(CW(), dict(waveform=dict(type="cw", frequency_hz=0, samples=self._samples, amplitude=0, antennas=antennas), real=[1.0] * 8, imag=[0.0] * 8, delay_samples=[0.0] * 8))]
                board_plan.append((antennas, records))
            plans.append(board_plan)
        return plans, targets
