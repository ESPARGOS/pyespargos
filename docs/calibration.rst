Calibration
===========

ESPARGOS sensors share a reference clock. To understand what this synchronizes
and what we still need to calibrate, we distinguish four offsets between radio
chains:

.. list-table::
   :header-rows: 1
   :widths: 12 32 56

   * - Offset
     - Meaning
     - Effect
   * - CFO
     - Carrier frequency offset
     - The relative carrier phase drifts over time.
   * - SFO
     - Sampling frequency offset
     - The sample grids drift apart over time.
   * - STO
     - Sampling time offset
     - The sample grids start at different times.
   * - CPO
     - Carrier phase offset
     - The carriers have different phases even at the same frequency.

Within a synchronized array, the shared clock removes relative CFO and SFO
when we use the same carrier frequency and sample rate. We therefore do not
need to calibrate these two offsets between its sensors. This also applies to
combined arrays that share a reference clock. An external transmitter can
still have a frequency offset relative to the array.

The challenge is that a shared clock does not remove STO and CPO. Each receiver
and transmitter still introduces a hardware-dependent phase offset and delay.
Two receivers can report different phases for the same input, and identical
digital samples can leave two transmitters at different phases or times. We
measure and compensate for these offsets so that we can use the phase and
timing differences between antennas to estimate direction or form a transmit
beam.

We can measure the offsets with either WiFi reference packets or broadband IQ
pilots. In both cases, we want to measure the phase and timing differences
between the receive or transmit chains involved.

Phase, delay and amplitude
--------------------------

We choose one antenna as the reference. For the same input, we model another
antenna's complex baseband signal as

.. math::

   y(t) = g\,e^{j\phi} x(t-\tau),

where :math:`g > 0` is the relative gain, :math:`\phi` is the carrier phase
offset (CPO), and :math:`\tau` is the relative delay, including the sampling
time offset (STO).
Positive :math:`\tau` means that the signal arrives later.

.. figure:: _static/calibration-offsets.svg
   :alt: A complex sample rotated by 45 degrees, a unit impulse delayed by 10 ns, and the resulting relative phase across frequency before and after correction.

   Phase rotation and delay for :math:`g=1`, :math:`\phi=45^\circ` and
   :math:`\tau=10\,\mathrm{ns}`. Correcting the phase removes the constant
   offset. Correcting the delay also removes the slope across frequency.

Using :math:`X(f)=\int x(t)e^{-j2\pi ft}\,dt`, we obtain the relative response

.. math::

   H(f) = \frac{Y(f)}{X(f)}
        = g\,e^{j(\phi-2\pi f\tau)},
   \qquad
   \angle H(f) = \phi-2\pi f\tau \pmod{2\pi}.

Here :math:`f` is the **frequency offset from the carrier**.
The phase intercept at :math:`f=0` gives :math:`\phi`, and the slope is
:math:`-2\pi\tau`. In the example, correcting only the 45° offset leaves
an error of −72° at :math:`f=20\,\mathrm{MHz}`.

We correct both terms by multiplying by

.. math::

   C(f) = e^{-j\phi} e^{+j2\pi f\tau},
   \qquad C(f)Y(f) = gX(f).

The gain :math:`g` remains: ESPARGOS corrects relative phase and delay, but
does not equalize receiver gains, transmit power or antenna patterns.

The calibration types
---------------------

The paper `Phase Calibration of Distributed Antenna Arrays
<https://arxiv.org/html/2304.05144v1>`_ by Larsson and Vieira (2023) is a good
first overview of array calibration. We use their terminology from Sections II
and III to distinguish the following calibration types:

.. list-table::
   :header-rows: 1
   :widths: 12 46 42

   * - Type
     - What is known
     - What it enables
   * - FR
     - Relative receive-chain phases
     - Receive beamforming and angle estimation using array geometry
   * - FT
     - Relative transmit-chain phases
     - Transmit beams in a chosen physical direction
   * - R
     - Relative TX/RX relationships between antennas
     - Derive transmit weights from a received pilot using reciprocity
   * - F
     - FR and FT, plus the TX–RX phase relationship within each antenna
     - A common phase reference for reception and transmission

.. figure:: _static/calibration-types.svg
   :alt: F implies FR plus FT, which in turn provides FR, FT and R. Arrows point from more information to less information.

   An arrow means that the starting calibration also provides the information
   of the type it points to, for the same antennas and operating configuration.
   The reverse does not generally hold.

Which antennas does a calibration cover?
----------------------------------------

In pyespargos, a ``Pool`` groups one or more ESPARGOS boards so that we can
configure their sensors and process their measurements together.

We do not have to calibrate the whole pool at once. For example, we can put
four sensors on WiFi channel 1 and four on channel 11. We then calibrate each
channel separately, with its own phase reference. In a mixed IQ array, we can
instead calibrate a group of receivers and a group of transmitters.

.. figure:: _static/calibration-coverage.svg
   :alt: Calibration can cover both arrays together, just three antennas, two groups at different frequencies, or separate RX and TX groups.

   Each color marks one group within an example. Matching colors across the
   two arrays mean that those antennas were calibrated together. Gray antennas
   are not covered. The examples are independent of one another.

In Python, a ``Calibration`` holds one or more ``CalibrationGroup`` objects.
Each group covers a particular set of antennas, one direction (RX or TX), one
frequency and one sample rate. Its phase reference is shared within that group.

Two independently calibrated boards are not automatically coherent together.
To use them as one aperture, we measure them against a common reference across
both boards. Similarly, storing two groups in the same ``Calibration`` object
does not align their references. Taking a subset of an existing group does
preserve its reference.

.. figure:: _static/calibration-objects.svg
   :alt: Calibration contains CalibrationGroups, each containing AntennaCorrections with a CalibrationState and Antenna identity.

   The Python objects store per-antenna RX or TX phase and delay corrections,
   together with the state for which each correction is valid.

An ``Antenna`` identifies a controller address and a hardware antenna ID, 0–7.
These IDs are not row/column indices. Use the board revision's mapping when
indexing array-shaped CSI or IQ data.

Choose what to calibrate
------------------------

``pool.calibrate()`` reads the current sensor modes: WiFi and IQ RX sensors
need receive calibration, while IQ TX sensors need transmit calibration.
``pool.select(...).calibrate()`` returns corrections for just that selection.
Both return a ``Calibration`` and merge successful measurements into
``pool.calibrations``. Merely selecting antennas does not change their modes.

WiFi measurements can cover several channels in one call. IQ measurement
currently requires a common center frequency and RX sample rate across the
connected IQ sensors. TX sensors also need prepared waveforms with the same
replay length. Store IQ measurements at different frequencies as separate calibration groups.

IQ calibration uses the connected reference domain as helpers, including
sensors outside the selection. It restores their modes afterward, but may
change their acquisition epochs. Check validity before reusing an older result.

How to measure calibration
--------------------------

Receive calibration
~~~~~~~~~~~~~~~~~~~

We send a reference signal from the controller through the REF distribution
network and measure it at each sensor. We then subtract the modeled
distribution delays to recover the relative receive phase and timing. This
gives us **FR calibration**.

For WiFi calibration, we use reference packets. For IQ receive calibration,
we use a broadband reference signal. In either case, we remove receiver
offsets while preserving phase differences caused by the external signal.

Combined arrays need a shared clock, a common RF reference and the correct
reference-path model, including external cable lengths. See
:doc:`combined-arrays` for wiring and configuration.

Transmit calibration
~~~~~~~~~~~~~~~~~~~~

We can measure the transmit offsets in two ways. In both cases, we start with
receive calibration.

.. figure:: _static/calibration-paths.svg
   :alt: Forward reference calibration followed by controller-monitor measurements or reciprocal sensor pilot exchanges

   Arrows show calibration signals. The shared clock and control connections
   are omitted.

**Controller monitor.** We have the sensors transmit known pilots back through
REF and measure them with the controller's receiver. The monitor contributes
the same receive response to every measurement. We subtract the modeled REF
delays to obtain relative sensor TX phase and delay, giving us **FT
calibration**. For this method, we need a bidirectional RF path.
An amplifier that passes signals only from controller to sensors can block it.

.. warning::

   Aperture kits use a unidirectional amplifier-splitter in the REF network,
   so we cannot use controller-monitor TX calibration with these kits. Use
   reciprocal sensor calibration instead.

**Reciprocal sensor links.** We exchange pilots between sensors in both
directions, either over the air or through weak coupling between REF
distribution ports. By comparing both directions, we cancel the coupling,
provided it is reciprocal and stable during the measurement. We need enough
usable links to connect the selected sensors.
The measurement starts with two reference sensors. If their links leave part
of the array disconnected, it tries additional reference sensors using the
links already observed. We check the fitted phase and delay
corrections with separate captures before applying them.

We first remove the RX response that we measured with forward REF calibration.
From the reciprocal comparison, we can then recover the relative TX response.
The complete procedure provides **FR + FT**. The initial RX calibration uses
the forward REF signal for both REF and over-the-air pilot exchanges.

For IQ, we use the timing anchors from receive synchronization to start TX
replay on the same sample grid. We therefore have coarse TX–RX time alignment,
but this does not establish the carrier-phase relationship between TX and RX.
Neither method measures that remaining common phase offset, so we obtain
**FR + FT**, rather than strict **F calibration**. We do not need that common
phase offset for relative receive processing or geometric transmit steering.

Our accuracy depends on the reference paths. If we use the wrong cable delay,
we can mistake it for a sensor delay. Differences between the REF route and an
antenna-port route can also leave residual errors. Calibration does not replace
a measurement of antenna patterns or the surrounding propagation environment.

WiFi: measure and apply a calibration
-------------------------------------

The following example assumes the sensors are in WiFi mode and the board is
configured to generate its internal reference during calibration. Transmit
authorization must be enabled for the reference generator.

``pool.calibrate()`` delegates WiFi measurements to
``pool.wifi.calibrate_channels()``, which groups sensors by their actual primary
and secondary channels. It visits each channel with the reference generator
without retuning the receivers. ``duration`` is the capture time **per channel**.

.. code-block:: python

   from espargos import Pool

   with Pool("172.16.0.241") as pool:
       pool.configure(mode="wifi", channel=6, secondary_channel=0)
       pool.select(antennas=[0, 1, 2, 3]).configure(channel=1)
       receiver = pool.wifi
       receiver.set_csi_acquisition_config({"acquire_csi_force_lltf": True})
       result = pool.calibrate(duration=2)
       states = receiver.get_calibration_states()

       def receive(cluster):
           calibrated = receiver.apply_calibration(
               cluster, "lltf", calibration=result, states=states,
           )
           # Shape: (boards, rows, columns, subcarriers).
           # Missing or unselected antennas contain NaN.
           print(calibrated.shape)

       receiver.add_callback(
           receive,
           callback_predicate=lambda c: c.has_lltf and c.completion.sum() == 4,
       )
       for _ in range(100):
           pool.run(timeout=0.05)

Refresh ``states`` after configuration changes. Passing it explicitly avoids
HTTP reads inside the callback. If omitted, ``apply_calibration()`` reads the
current state itself. An uncalibrated channel or a stale selected antenna
raises ``KeyError``. The returned CSI has the normal timestamp and gain-phase
corrections from ``CSICluster`` deserialization as well as the measured
calibration correction.

Within the same ``with`` block, we can calibrate a subset of antennas by ID::

   result = pool.select(antennas=[0, 2, 5]).calibrate(duration=2)

WiFi calibration defaults to separate phase references for each board.
For a combined aperture sharing a reference, request ``per_board=False``
explicitly. Loading a layout does not select a shared phase reference.
``Pool.from_config()`` supplies the cable corrections, which require that
shared-reference setting:

.. code-block:: python

   from espargos import Pool

   with Pool.from_config("config/aperture-kit-8x4.yml") as pool:
       pool.configure(mode="wifi", channel=6, secondary_channel=0)
       result = pool.calibrate(per_board=False)

We can use either 20 MHz or bonded 40 MHz WiFi channels.

``pool.calibrations`` retains the scoped results. New measurements replace
only overlapping antenna/direction/frequency entries. Recalibrating a subset
creates a new reference group, so do not assume it is phase-aligned with the
antennas left out of that measurement.

.. figure:: _static/calibration-csi.svg
   :alt: Measured reference CSI phase before and after phase and delay calibration

   We check the calibration with a separate reference capture on one eight-sensor
   array, channel 6. We plot phase relative to sensor 0 at each frequency and
   account for the modeled REF distribution delays. After correction, the
   phase differences should approach zero.

IQ: measure and apply a calibration
-----------------------------------

For IQ reception, ``pool.calibrate()`` synchronizes the sample grids and
measures phase and delay using the broadband REF source. It chooses the
receive gain for the reference path.

The pool also stores the returned ``Calibration`` in ``pool.calibrations``.
``pool.iq.calibration`` holds the dense receive model used by
``cal_correction()``. We apply the correction to the raw IQ samples ourselves:

.. code-block:: python

   import numpy as np
   from espargos import Pool

   with Pool("192.168.1.2") as pool:
       pool.configure(mode="iq_rx", frequency_hz=2437e6,
                      sample_rate_hz=80e6, output="antenna_r")
       receiver = pool.iq
       result = pool.calibrate()

       def receive(cluster):
           if not receiver.calibration_applies_to(cluster):
               raise RuntimeError("IQ capture does not match the calibration")
           spectrum = np.fft.fftshift(np.fft.fft(cluster.iq, axis=-1), axes=-1)
           spectrum *= receiver.cal_correction(fft_size=cluster.iq.shape[-1])
           print(spectrum.shape)

       receiver.add_callback(receive)
       for _ in range(100):
           pool.run(timeout=0.05)

The lower-level ``receiver.sync()`` performs the IQ receive measurement directly
and returns a ``SensorCalibration``. Use ``pool.calibrate()`` for mixed sensor
roles and antenna selections.

Recalibrate after changing the frequency or sample rate. The accepted
receiver tuning range is 2310–2640 MHz, but synchronization also depends on
reference signal quality. A valid tuning frequency does not guarantee that
reference synchronization will succeed. The web interface's
calibration result is not shared with Python. The signal analyzer can apply
its own calibration to the phase display.

See :doc:`iq-reception` for capture setup and trigger modes.

For IQ transmission, we first configure sensor modes and source waveforms.
The pool uses reciprocity by default, but we can select either method::

   result = pool.calibrate(tx_method="monitor")
   # Or exchange pilots between sensors:
   result = pool.calibrate(tx_method="reciprocity", path="reference")

With reciprocity, choose ``path="antenna_l"`` or ``"antenna_r"`` for OTA
measurements. TX calibration applies the corrections on the sensors, so do
not apply them again to uploaded waveforms. Each run uses one common TX
frequency and replay length.

See :doc:`iq-transmission` for a complete ``Pool`` example, waveform
selection and steering for single and combined arrays.

Validity and reuse
------------------

We need to recalibrate after retuning, switching sensor modes, restarting
acquisition or changing the TX replay length. These operations can change phase or timing.
Returning to the previous settings does not restore the previous calibration.

``CalibrationState`` stores the antenna, RX/TX direction, frequency, sample rate,
boot ID and acquisition start identifier (the epoch). For IQ TX it also stores
the replay length. The library checks these fields when looking up a correction.

For IQ, the epoch comes from the hardware RX/TX start timestamp. WiFi CSI does
not carry an equivalent epoch. Its API uses a receiver-local generation and checks
current channel, mode and boot information. After changing settings outside the
pool, call ``pool.wifi.invalidate_calibration()`` and recalibrate. In particular,
retuning away and back cannot be detected from a final channel read alone.
WiFi state snapshots belong to the pool that created them. When passing cached
``states`` to ``apply_calibration()``, refresh them after configuration changes
or reconnects. The function cannot detect later hardware changes from cached
values.

We can use ``result.select(antennas=..., direction=..., frequency_hz=...)`` to
extract a subset, and ``older.merge(newer)`` to combine stored results. Neither operation
creates coherence between independently measured groups.

For custom processing, ``result.correction(state)`` returns an
``AntennaCorrection`` or raises ``KeyError`` if no matching measurement exists.
Its ``response(frequencies_hz)`` gives complex correction factors at absolute
frequencies. The factors contain phase and delay only. We apply them once. ``result.diagnostics`` provides the measurement method and any available
fit diagnostics.
