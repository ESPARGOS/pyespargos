Getting Started
=================

ESPARGOS is an antenna array for coherent radio measurements and transmission.
With *pyespargos*, we can receive WiFi channel estimates, capture raw I/Q
samples, and transmit custom waveforms with beam steering.

Choose what you want to do
----------------------------

* :doc:`wifi-reception` and :doc:`wifi-transmission`: measure channel state
  information (CSI) from WiFi packets and send scheduled packets for channel
  sounding, direction finding and sensing.
* :doc:`iq-reception`: capture raw complex samples without WiFi packet
  decoding. We can trigger captures periodically or when a signal arrives.
* :doc:`iq-transmission`: transmit repeating waveforms, steer multiple beams,
  or assign different signals to individual antennas.
* :doc:`iq-radar`: combine IQ transmission and reception to measure range,
  direction and motion, with a calibrated range origin.

We can combine these modes within one board or across several boards. For
example, some sensors can transmit IQ waveforms while others receive IQ
samples, and another group can receive WiFi packets. Each sensor has one mode
at a time, but different sensors can operate in different modes simultaneously.

Boards, pools and sensor roles
--------------------------------

Each ESPARGOS board contains eight **sensors**, which receive or transmit the
antenna signals, and a **controller**, which connects them to the computer
and contains the reference radio. Each board has its own controller and IP
address.

In a synchronized multi-array setup, one board is the **master**. Its
controller supplies the shared clock and calibration reference. When we talk
about the **reference monitor** or the shared **controller reference signal**,
we mean this master's controller. On a standalone array, its own controller
performs those functions. The other boards still have controllers that manage
their sensors and network connections.

A :class:`Board <espargos.board.Board>` represents one eight-sensor ESPARGOS array. A :class:`Pool <espargos.pool.Pool>` manages one
or more boards, including their connections, configuration and calibration.
We normally start with a pool, even for one board::

   from espargos import Pool

   with Pool("192.168.1.2") as pool:
       pool.configure(mode="wifi")
       receiver = pool.wifi
       # Register callbacks and process incoming measurements here.

Each sensor can operate in WiFi, IQ RX or IQ TX mode. We can select subsets
of antennas and assign different roles or frequencies within the same pool.
:attr:`pool.wifi <espargos.pool.Pool.wifi>`, :attr:`pool.iq <espargos.pool.Pool.iq>` and :attr:`pool.tx <espargos.pool.Pool.tx>` share that pool's connections.
See :doc:`pool` for selections and mixed configurations.

For larger apertures, we can load the board arrangement from a configuration
file using :meth:`Pool.from_config() <espargos.pool.Pool.from_config>`. A shared clock and reference network allow
coherent measurements across boards. See :doc:`combined-arrays` for wiring
and layout configuration.

Calibration and demos
-----------------------

Calibration measures the phase and timing differences between sensors. We
apply these corrections when comparing received signals or steering transmit
beams. :doc:`calibration` explains the available methods and how to calibrate
all antennas or separate groups.

The reception and transmission pages include runnable examples. For an
interactive view, try the ``instantaneous-csi``, ``signal-analyzer`` or
``transmit-beamforming`` demos. :doc:`application-framework` explains how
to reuse their controls in your own Qt application.
