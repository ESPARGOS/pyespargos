# IQ signal analyzer

Run from a pyespargos checkout with the standard demo dependencies installed:

```sh
python demos/iq-signal-analyzer/iq-signal-analyzer.py <host>[,<host>...]
```

The analyzer displays power and phase waterfalls, time-domain I/Q traces,
constellations, and spectra. Its receiver drawer configures each capture's
frequency, sample rate, gain, RF switch, and Interval/Accumulate/Signal trigger.

Startup acquires one WiFi reference packet across the array before entering
IQ mode. Boards need IQ-capable firmware and a working reference path; coherent
multi-board capture also needs the shared clock/reference distribution.
Fine time/phase calibration is available from the receiver drawer. Exiting the
application restores WiFi/CSI mode and stops the reference tone.

Signal triggering coordinates the eight antennas within one array. Use a
single-board setup for Signal capture. In a combined multi-array setup, each
array triggers independently, even with a shared clock and reference, so their
captures may not cover the same event. Use Interval or Accumulate for
synchronized captures across multiple arrays.
