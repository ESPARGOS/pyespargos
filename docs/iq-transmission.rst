IQ Transmission Calibration
===========================

The Aperture Kit needs a shared TX calibration across all four boards. Sensors
exchange pilots through the REF network, with their antennas disconnected.
The pilots travel between splitter outputs, so they do not need to pass
backwards through the amplifier to reach the controller monitor.

Connect all boards to the same REF network and enable transmit authorization
on each controller. With matching current firmware, run::

   ./demos/iq-tx-calibrate/iq-tx-calibrate.py 192.168.1.3,192.168.1.4,192.168.1.5,192.168.1.6 --frequency 2380

Choose a frequency you are permitted to use. The command selects IQ TX on all
sensors and preserves their source waveforms. Sensors without a waveform
receive a quiet complex tone.

The command prints the corrections and leaves TX calibrated but silent. Start
transmission from the web interface. Changing frequency, restarting replay or
rebooting requires calibration again.

The web interface can calibrate devices in Internal or Master reference mode.
Use the Python library to calibrate Slave devices together with their master.

For a mixed RX/TX setup, configure the sensor modes and waveforms first, then
call ``pool.calibrate_tx()`` on an ``IQPool`` containing all boards. Selected
transmitters need a common frequency and sample count. Active receivers must
use that frequency and a common receive sample rate. The method restores modes,
waveforms, steering and mute settings, and keeps RX calibration for the sensors
returning to IQ RX. A failed calibration leaves transmitters silent.

If the REF outputs cannot communicate, use ``--over-the-air`` or
``pool.calibrate_tx(pilot_output="antenna_l")`` to send pilots through the left
antennas instead. Use ``--pilot-output antenna_r`` or
``pool.calibrate_tx(pilot_output="antenna_r")`` for the right antennas.
The same procedure uses the selected path for both transmission and reception.
Both options still require the shared clock and forward REF for RX calibration.
Keep the arrays and nearby objects still during that
measurement. Calibration never switches to antenna transmission automatically.
Weak or clipping paths may need a different receiver gain. Set ``--rx-gain``
in the demo or pass ``rx_gain`` to ``calibrate_tx()`` to override the default.

How it works
------------

First, the forward REF signal calibrates the receivers. Two selected sensors
then send broadband pilots, followed by groups of up to eight transmitters
using separate pilot subcarriers. The reference sensors receive those groups.
Comparing both directions at the same frequencies cancels each reciprocal path's
propagation response. The second reference provides redundant measurements.

This gives relative TX phase and delay without measuring the full antenna
response or knowing the distances between sensors. RX calibration accounts
for the REF cable and PCB trace delays. The four-board kit uses six pilot
captures, rather than measuring each of its 32 transmitters separately.
Temporary pilots use 1024 samples, independently of the saved user waveform.
Calibration uses its own TX gain (A = 0, B = 0) and 40 MHz bandwidth, with
stronger pilots for inter-board REF paths. Simultaneous OTA pilots use a lower
amplitude to avoid clipping nearby receivers. The two broadband reference
pilots stay stronger so distant receivers can detect their individual tones.
Your transmitter settings are restored
afterwards, including when calibration fails.

The delay fit correlates all surviving pilot tones before fitting phase and
delay. RX synchronization selects the unambiguous delay interval. The final
fit rejects isolated contaminated tones without unwrapping through them.
At least 90% of the candidate tones must agree, spanning at least 10 MHz.
The final fit uses all usable links and rejects inconsistent measurements.
The reported residuals describe agreement between links, not absolute
calibration accuracy. Calibration needs a connected set of reciprocal links
with enough signal and no receiver clipping.

This still requires coordination between sensors. It is not an on-chip
loopback calibration.

The web interface automatically uses REF reciprocity when external REF is selected.
It calibrates the sensors on that controller. Use the command above to obtain
one common TX calibration across the complete Aperture Kit.

Reusable TX sessions
--------------------

``espargos.iq_tx`` provides the TX operations independently of Qt and the demo.
``TXLayout`` describes the aperture, and ``TXSession`` handles waveform synthesis,
file uploads, steering, calibration and the reference monitor. Calls are blocking:
use one worker for a GUI, and serialize operations on a session. Boards are updated
in parallel internally.
Sessions created without an existing pool use WebSocket streaming for reliable
calibration bursts and monitor data. An attached pool keeps its own transport.

.. code-block:: python

   from espargos import TXLayout, TXSession
   from espargos.iq_tx import default_beam

   layout = TXLayout(config="config/aperture-kit-8x4.yml")
   with TXSession(layout) as tx:
       tx.modes([["iq_tx"] * 8 for _ in tx.boards])
       tx.settings({"frequency_hz": 2380000000, "output": "reference"})
       beam = default_beam(len(tx.boards))
       tx.apply([beam], samples=1024, frequency=2380e6)
       tx.calibrate()
       beam["u"] = 0.2  # horizontal direction cosine
       tx.apply([beam], samples=1024, frequency=2380e6)
       # tx.mute(False) starts output; tx.mute(True) silences it.

The session starts muted and closing it mutes the transmitters and stops the
monitor. Transmit authorization must already be enabled on each controller.
Pass ``pool=existing_iq_pool`` to share a running pool and its connections.
Closing an attached session leaves the borrowed pool and boards open.
After a lost connection, ``reconnect()`` mutes TX, reopens the streams and reads
the current device state. It does not resume transmission. Recalibrate after a
device reset before starting again.

Use ``default_beam`` as a starting point for waveform and steering parameters.
``apply`` accepts up to four beams, sharing the sample count and frequency.
Sources are cached on the sensors, so changing steering does not upload samples.
``configure_monitor`` enables the monitor on the detected master and returns a
``TXMonitor`` whose ``latest`` attribute holds the most recent complete burst.
