Getting Started
===============

.. warning::

   Bluetooth, IQ transmit streaming and continuous IQ receive streaming are experimental and under development.
   The associated demos are unstable, and the API will change without notice.

ESPARGOS One is a 2.4 GHz array with eight antennas. Each antenna has a
radio called a sensor. The controller connects the sensors to your
computer. You can open its IP address in a browser to configure the
array, or connect to the same address from Python with *pyespargos*.

Follow the `installation instructions <https://github.com/ESPARGOS/pyespargos#installation>`_
for your operating system. Use your controller's address in place of
``192.168.1.2`` in the examples.

Receive or transmit
-------------------

* Use :doc:`wifi-reception` to measure how a Wi-Fi signal arrives at each
  antenna. Use :doc:`wifi-iq-reception` to capture the packet waveform at
  each antenna, for example to study an 802.11b preamble.
  :doc:`wifi-transmission` can send packets on a schedule for those measurements.
* Use :doc:`ble-reception` to find nearby Bluetooth LE advertisers or
  compare the phase of their packets across antennas.
* Use :doc:`iq-reception` to capture the waveform itself as a sequence of
  complex I/Q samples. This works with signals that are outside the Wi-Fi
  and Bluetooth packet formats.
* Use :doc:`iq-transmission` to repeat a waveform from sensor memory.
  Use :doc:`iq-transmit-streaming` to send changing samples, symbols or
  packets from your computer while the array transmits.
* Use :doc:`iq-radar` to transmit a known waveform and measure its reflections
  for range, direction and motion.

Connect to an array
-------------------

In Python, a :class:`Board <espargos.board.Board>` represents one ESPARGOS
array. A :class:`Pool <espargos.pool.Pool>` manages connections and calibration
for one or more boards. Start with a pool even for one board. This example
connects to the controller and puts all eight sensors into Wi-Fi reception::

   from espargos import Pool

   with Pool("192.168.1.2") as pool:
       pool.configure(mode="wifi")
       receiver = pool.wifi
       # Register callbacks and process incoming measurements here.

See :doc:`pool` for antenna selection and :doc:`modes` to choose what
each sensor and the controller radio do.

To use several boards as a single phase-coherent array, connect them to a
shared clock and load their layout with
:meth:`Pool.from_config() <espargos.pool.Pool.from_config>`.
See :doc:`combined-arrays` for the wiring and layout file.

Calibration and demos
---------------------

Two sensors can record the same signal with different phase and timing
because of their radio circuits. Calibration measures those differences.
Calibrate before using phase to find a direction or steering a transmit
beam. Start with :doc:`calibration` to choose a method for your setup.

The reception and transmission pages include runnable examples. For an
interactive view, try the ``instantaneous-csi``, ``signal-analyzer`` or
``transmit-beamforming`` demos. :doc:`application-framework` explains how
to reuse their controls in your own Qt application.
