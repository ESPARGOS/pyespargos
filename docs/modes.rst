Modes
=====

Each ESPARGOS board has eight sensor radios and a separate radio on the
controller. Choose a sensor mode based on what you want to measure or
transmit. Choose a controller mode based on the reference signal you need.
The controller continues to forward sensor data in every mode.

Sensor modes
------------

**WiFi**
   Measure the channel state information (CSI) that the WiFi modem reports
   for received packets. The same mode can also send scheduled WiFi packets
   and receive between transmissions. See :doc:`wifi-reception` and
   :doc:`wifi-transmission`.

**WiFi IQ**
   Capture the IQ waveform around a detected WiFi packet. Use
   ``pool.wifi_iq`` to estimate CSI from training symbols or subscribe on
   the board for the original samples. See :doc:`wifi-iq-reception`.

**BLE**
   Capture IQ samples of Bluetooth LE packets on one selected channel per
   board. Use the captures to identify packets or compare their phase across
   antennas. See :doc:`ble-reception`.

**IQ RX**
   Capture a waveform at a chosen center frequency without requiring a WiFi
   or BLE packet. Use this for signals with an unknown or custom modulation,
   or when you want to choose the capture trigger yourself. See :doc:`iq-reception`
   for triggered wideband captures and :doc:`iq-receive-streaming` for
   continuous narrowband samples.

**IQ TX**
   Transmit a waveform from a repeating buffer or a live stream. A sensor
   enters this mode muted. Both beamforming and streamed BLE transmission
   use IQ TX. See :doc:`iq-transmission` and :doc:`iq-transmit-streaming`.

Controller modes
----------------

The controller has one radio to generate a reference for the sensors or to
monitor a signal on the reference path. Its mode can differ from theirs.
For example, the controller can send WiFi packets while the sensors record
them in IQ RX.

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Controller mode
     - What the radio can do
   * - WiFi
     - Send reference packets on the configured WiFi channel and bandwidth.
   * - WiFi IQ
     - Send 20 MHz WiFi reference packets on the channel selected for WiFi IQ capture.
   * - BLE
     - Send LE 1M reference advertisements for BLE measurements.
   * - IQ
     - Generate a continuous tone or wideband noise, or receive IQ on the reference path. Monitoring and generation use the same radio and cannot run together.

Selecting a mode makes the corresponding reference available. Whether it
transmits depends on the reference settings. In Python, read the current
selection with ``board.control.get_json("get_controller_mode")``. To change
just the controller radio, use, for example,
``board.control.post_json("set_controller_mode", {"mode": "wifi_iq"})``.
The other values are ``wifi``, ``ble`` and ``iq``.

A controller-only master
------------------------

A board can supply the shared reference while the other arrays do all the
receiving or transmitting. Give its address separately when you open the
pool. For example, this setup has two receiving boards and 16 antennas::

   from espargos import Pool

   with Pool(["192.168.1.3", "192.168.1.4"],
             reference_controller="192.168.1.2") as pool:
       pool.configure(mode="wifi", channel=6)
       calibration = pool.calibrate(per_board=False)

Configure the master and slave reference connections as described in
:doc:`combined-arrays`. The pool applies sensor modes to the two receiving
boards and waits for their antennas. It uses the master's controller radio
for the reference. Its eight sensors keep their existing roles. You can
access the master directly as ``pool.reference_controller``.

In a layout file, put ``reference_controller: 192.168.1.2`` under
``combined-array``, alongside ``boards`` and ``array``. List only boards that
contribute antennas in ``boards``. If the master also contributes antennas,
include it in ``boards`` like any other array.

Mixing roles
------------

Use ``pool.configure()`` to assign a mode to all sensors, or
``selection.configure()`` to change only a subset. Sensor modes are
``wifi``, ``wifi_iq``, ``ble``, ``iq_rx`` and ``iq_tx``.

For example, to receive WiFi CSI on antenna 1 of each board in an 8×4
aperture kit and raw IQ on the other 28 antennas::

   from espargos import Pool

   with Pool.from_config("config/aperture-kit-8x4.yml") as pool:
       pool.configure(mode="iq_rx", frequency_hz=2437e6, sample_rate_hz=80e6)
       pool.select(antennas=[1]).configure(mode="wifi", channel=6)

       pool.iq.add_callback(handle_iq)
       pool.wifi.add_callback(handle_csi)
       pool.start_processing()
       input("Press Enter to stop\n")

Here ``handle_iq`` and ``handle_csi`` are your measurement callbacks. By
default, each receiver waits for the antennas in its own mode. Results keep
the pool's antenna layout, with gaps for antennas that did not contribute.
To calibrate both groups across the kit, call ``pool.calibrate(per_board=False)``
after assigning the modes. See :doc:`calibration` for the reference connections.

Each sensor has one role at a time. If two selections overlap, the later
mode change takes effect on the shared antennas. Some settings apply to the
whole board. In particular, select all eight antennas when changing the IQ
receive sample rate or input port. That is why the example sets the sample
rate before assigning the four WiFi receivers.
