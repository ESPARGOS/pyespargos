IQ Receive Streaming
====================

For a narrowband signal that may last seconds or minutes, ESPARGOS can
deliver consecutive complex samples from every receiving antenna. For
example, you can follow a carrier as it drifts or demodulate a signal that
crosses many block boundaries. For short events that need several MHz of
bandwidth, use the triggered captures in :doc:`iq-reception`.

Continuous capture uses the downsample trigger in IQ RX mode. The Bluetooth
capture path supplies 1.6 MSa/s through the 1.2 MHz analog filter. Each sensor
digitally low-pass filters those samples before reducing them to the requested
output rate. The SDK selects this input configuration when you request one of
the output rates below.

The IQ receivers on a board share a sample rate. Each sensor can tune to
a different center frequency. Choose the lowest rate that covers your signal
and leave the filter's transition band near each Nyquist edge clear.

.. list-table::
   :header-rows: 1

   * - Rate per antenna
     - Usable frequency offsets
     - I/Q data from eight antennas
   * - 16 kSa/s
     - ±6 kHz
     - 0.51 MB/s
   * - 32 kSa/s
     - ±12 kHz
     - 1.02 MB/s
   * - 64 kSa/s
     - ±24 kHz
     - 2.05 MB/s
   * - 80 kSa/s
     - ±30 kHz
     - 2.56 MB/s
   * - 100 kSa/s
     - ±37.5 kHz
     - 3.20 MB/s

.. figure:: _static/iq-rx-stream-response.svg
   :alt: Measured response at all five sample rates, with a close-up of the passband.

   The shaded band shows where to place your signal. The dashed lines mark
   the Nyquist limits. Each point comes from an RF tone swept past the array
   at 2441 MHz. Each antenna's response is relative to its in-band level.
   The last panel compares the median response inside the usable band.

The radio tunes in 1 kHz steps. Allow for the frequency error between
the transmitter and receiver when choosing the center of a narrow passband.
At the low end of the 2310–2640 MHz tuning range, reception can
vary between sensors. On our array all eight received a test carrier at 2316 MHz,
while six received it at 2310 MHz.

On the same array, moving a test tone by one or two output sample rates,
or by 1.6, 3.2 or 6.4 MHz in either direction, reduced its folded signal by
at least 57 dB at all five rates. A strong tone *inside* the passband can
also have a weak mirror on the opposite side of the center frequency due to
I/Q imbalance. The largest such mirror we measured was about 34 dB below
the tone.

A peak at exactly zero frequency can come from the receiver's DC offset.
To distinguish it from a carrier, retune slightly. A received carrier moves
by the tuning offset, while the receiver's DC component stays at zero.

Receive a stream
----------------

This example tunes all eight sensors to 2437 MHz and prints the RMS amplitude
of each antenna in ADC counts. Replace the hostname with your controller's
address. Calibration uses the array's reference signal to align the receivers
before capture.

.. code-block:: python

   import numpy as np
   from espargos import Pool

   def report(block):
       rms = np.sqrt(np.mean(abs(block.iq) ** 2, axis=-1))
       if block.first_sample % 16000 == 0:
           print(block.first_sample, rms.round(1))

   with Pool("myspargel.local") as pool:
       pool.configure(mode="iq_rx", frequency_hz=2437e6,
                      sample_rate_hz=16000, rx_gain=30, output="antenna_r")
       pool.calibrate()
       pool.iq.add_stream_callback(report)
       pool.start_processing()
       input("Receiving. Press Enter to stop. ")

Each block contains 128 samples per antenna, or 8 ms at 16 kSa/s.
``block.iq[board, row, column]`` is a complex NumPy vector.
The samples retain their measured amplitude and phase. To compare phase
between antennas, get the correction once after calibration and apply it on
the same frequency grid as the FFT. For example, the following can replace
the callback registration in the example above:

.. code-block:: python

   receiver = pool.iq
   correction = receiver.cal_correction(128)

   def compare_phase(block):
       if block.first_sample % 16000 == 0 and receiver.calibration_applies_to(block):
           spectrum = np.fft.fftshift(np.fft.fft(block.iq, axis=-1), axes=-1)
           calibrated = spectrum * correction
           # Phase relative to antenna (row 0, column 0) on the first board.
           phase = np.angle(calibrated * calibrated[0, 0, 0].conj())
           print(phase[..., 80])  # +2 kHz at 16 kSa/s

   receiver.add_stream_callback(compare_phase)

Choose a fixed gain before calibrating for phase measurements. Each receiver
is measured at its operating gain. An automatic gain change can shift phase
during reception. Manual gain edits preserve the capture epoch and stored
correction. After changing center frequency or sample rate, recalibrate
and get a new correction. See :doc:`calibration` for reference
cable settings.

Recording consecutive samples
-----------------------------

The data rates above exclude packet headers. Use wired Ethernet for all eight
antennas at 100 kSa/s. For a recording over a link that sometimes drops packets,
open the pool with ``Pool("myspargel.local").open(transports=["websocket"])``.
The underlying TCP connection retransmits lost network data. A long network
stall can still fill the sensor buffers, so check the sample indices with
either transport.

Place each block at ``block.first_sample`` in the recording. Adjacent blocks
are 128 samples apart. UDP can deliver blocks out of order, so a later
arrival may fill a gap. Keep any remaining gaps on the time axis. Joining
the samples across a gap introduces a false phase or frequency change.
Start a new recording segment after synchronization or a change of sample
rate or center frequency.

By default the callback receives a block when all active IQ receivers have
contributed. Pass ``include_partial=True`` to keep blocks with missing
antennas. Inspect ``block.completion`` to see which antennas contributed.
Missing samples are NaNs. Partial blocks may arrive after more recent
complete blocks, so use the sample index when assembling a recording.

For independently tuned sensors, or to inspect one sensor before array
synchronization, use
``board.iq.subscribe_stream(callback)``. Each message carries an
``antenna_id`` and a ``payload`` with the first sample index, sample rate
and a ``decode_iq()`` method. Free-running sensors have independent sample
indices. The pool groups synchronized antennas tuned to the same frequency.

Signal analyzer
---------------

From the SDK checkout, run the signal analyzer to inspect the time trace,
spectrum or waterfall. In the RX controls, select **Digital downsampling** as the
trigger mode and choose the **Output rate**.

.. code-block:: console

   $ python demos/iq/signal-analyzer/main.py myspargel.local
