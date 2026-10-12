IQ sampling
===========

ESPARGOS can capture raw complex (I/Q) samples, synchronized across its
antennas and boards. This uses an undocumented debug path in the ESP32 modem
that bypasses WiFi packet processing. Read about our discovery in the `ESP32
SDR write-up <https://espargos.net/espsdr/>`_.

Before you start
----------------

Make sure your controller is reachable and runs API major 4 with matching
sensor firmware. The example below uses ``192.168.1.2``. Replace it with your
board's address.

Start with one board and its internal reference source. For multiple boards,
you also need a shared clock and reference connection. See
:doc:`combined-arrays`.

Receive your first samples
--------------------------

Create an ``IQPool``, choose the receiver settings, and call ``sync()`` to
synchronize and calibrate the antennas:

.. code-block:: python

   import espargos

   board = espargos.Board("192.168.1.2")
   pool = espargos.IQPool([board])

   def handle_iq(cluster):
       print("Received IQ samples:", cluster.iq.shape)

   try:
       pool.start()
       pool.start_processing()
       pool.apply_config({
           "rf_freq_hz": 2437_000_000,
           "adc_decimation": 1,
           "rf_switch": 2,  # Receive from the antennas
           "trigger": {
               "mode": "interval",
               "interval_chunks": 16384,
               "offset_chunks": 0,
               "burst_chunks": 4,
           },
       })
       pool.sync()
       pool.add_iq_callback(handle_iq)
       input("Capturing. Press Enter to stop... ")
   finally:
       pool.stop_processing()
       pool.restore_wifi()
       pool.close()
       board.close()

The callback receives matching samples from all antennas. ``cluster.iq`` is a
complex NumPy array with shape ``(boards, rows, columns, 256)``. For one board,
that is ``(1, 2, 4, 256)``.

.. note::

   **Why 256 samples?** The firmware sends IQ samples in fixed blocks called
   *chunks*, each containing 256 complex samples per antenna. ``IQPool`` groups
   matching chunks from all antennas into one cluster. This is the transfer
   size, not a limit on capture length. For a longer waveform or FFT, combine
   consecutive chunks from the same capture burst. Four chunks give you 1024
   samples per antenna.

Both ``IQPool`` and ``CSIPool`` support background processing. Here,
``pool.start_processing()`` starts a thread that calls ``pool.run()`` for you.
Do not also call ``run()`` from your own loop while that thread is running.

How Calibration Works
---------------------

``pool.sync()`` sends a broadband reference signal to all receivers through the
board's (or multi-board) reference network. It uses this common signal to align
the samples in time and measure the phase and delay differences between
antennas. The results are stored in ``pool.calibration``. The example above
reads raw samples, while the signal analyzer can apply the calibration to its
phase display.

After calibration, your receiver settings are restored and the reference is
switched off. Run ``sync()`` again after changing the frequency or sample rate
(decimation). Calibration accepts the receiver tuning range, 2310-2640 MHz, and
has been tested up to 2640 MHz. At 2310 MHz, reference synchronization failed
on the test array. The web interface has an **RX Sync** button, but its
calibration is not shared with Python.

The Trigger Modes
-----------------

Set ``trigger`` to an object with a mode name and its settings. This replaces
all trigger settings, using defaults for fields you leave out. Omit ``trigger``
to keep the current settings. ``board.iq.get_config()["trigger"]`` returns the
full applied configuration, including defaults.

**Interval** captures short bursts at regular intervals. Start here for general
signal inspection. The example above captures four 256-sample chunks every
16384 chunks, with no offset. Receive the samples with ``add_iq_callback()``.

**Accumulate** averages repeated waveforms to make a weak repeating signal
easier to see. ``{"mode": "accumulate", "vector_chunks": 4, "interval_chunks":
32768, "offset_chunks": 0}`` selects four-chunk vectors and an output interval
of 32768 chunks. Use ``add_accumulation_callback()`` to receive the averages.

**Signal** waits for a signal to cross a threshold, then captures the same
event across all eight antennas. This is useful for short bursts such as WiFi
traffic. Use ``add_signal_capture_callback()`` with a single board. For
example:

.. code-block:: python

   pool.apply_config({"trigger": {
       "mode": "signal",
       "threshold": 64,
       "antennas": list(range(8)),
       "holdoff_ms": 300,
       "capture_chunks": 56,
       "quiet_threshold": 0,
   }})

These are also the Signal defaults. ``antennas`` lists which antennas may
request a capture. A threshold is measured in ADC counts (I or Q). Set
``quiet_threshold`` above zero to require a quiet period before a new onset.
Unknown fields and settings for a different mode are rejected.

Signal Analyzer Demo
--------------------

From a checkout with the demo dependencies installed, run:

.. code-block:: console

   python demos/iq-signal-analyzer/iq-signal-analyzer.py 192.168.1.2

Open **RX** to set the frequency, gain, and trigger mode. Use **App** to switch
between waterfalls, spectra, I/Q traces, and constellations.

.. figure:: _static/iq-signal-analyzer-signal.png
   :alt: Live power waterfalls from all eight antennas in Signal trigger mode.
   :width: 100%

   Signal-triggered traffic captured on an ESPARGOS array at 2437 MHz.
