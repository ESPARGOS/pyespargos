IQ Transmission Calibration
===========================

The Aperture Kit needs a shared TX calibration across all four boards. Its
amplified REF splitter only carries signals towards the sensors, so TX pilots
cannot travel back to the controller monitor.

Connect all boards to the same REF network and enable transmit authorization
on each controller. With matching current firmware, run::

   ./demos/iq-tx-calibrate/iq-tx-calibrate.py 192.168.1.3,192.168.1.4,192.168.1.5,192.168.1.6 --frequency 2380

Choose a frequency you are permitted to use. Calibration briefly transmits
pilots over the antennas. Keep the arrays and nearby objects still until it
finishes. The command selects IQ TX on all sensors and preserves their source
waveforms. Sensors without a waveform receive a quiet complex tone.

The command prints the corrections and leaves TX calibrated but silent. Start
transmission from the web interface. Changing frequency, restarting replay or
rebooting requires calibration again.

For a mixed RX/TX setup, configure the sensor modes and waveforms first, then
call ``pool.calibrate_tx()`` on an ``IQPool`` containing all boards. Selected
transmitters need a common frequency and sample count. Active receivers must
use that frequency and a common receive sample rate. The method restores modes,
waveforms, steering and mute settings, and keeps RX calibration for the sensors
returning to IQ RX. A failed calibration leaves transmitters silent.

How it works
------------

First, the forward REF signal calibrates the receivers. Each selected
transmitter then sends a pilot while the others receive. Comparing both
directions of each antenna link cancels its propagation response. This gives
relative TX phase and delay without measuring the full antenna response or
knowing the distances between sensors. The REF cable and PCB trace delays
are accounted for by the RX calibration.

The final fit uses all usable links and rejects inconsistent measurements.
The reported residuals describe agreement between links, not absolute
calibration accuracy. Calibration needs a connected set of reciprocal links
with enough signal and no receiver clipping.

The web interface automatically uses this method when external REF is selected.
It calibrates the sensors on that controller. Use the command above to obtain
one common TX calibration across the complete Aperture Kit.
