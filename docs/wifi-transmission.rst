WiFi Transmission
===================

ESPARGOS sensors can send WiFi packets on a repeating schedule. We use
``pool.wifi`` for both transmission and CSI reception: both operate in the
same ``wifi`` sensor mode. There is no separate WiFi TX mode.

A sensor cannot receive while actively transmitting a packet. Between
transmissions it resumes reception, even while its TX schedule remains
enabled. Other sensors can receive those packets and measure their CSI.
A transmitting sensor does not receive its own packet, so that packet’s CSI
cluster will be missing that sensor’s measurement.

.. warning::

   Enable transmit authorization in the controller settings only after
   confirming that you are permitted to transmit with the selected radio
   configuration.

Send periodic packets
-----------------------

This example sends a packet from sensor 0 every 100 ms on channel 6. The
remaining sensors receive only:

.. code-block:: python

   from espargos import Pool

   with Pool("192.168.1.2") as pool:
       pool.configure(mode="wifi", channel=6, secondary_channel=0)
       pool.wifi.set_tx_config({
           "active_by_antid": [True] + [False] * 7,
           "start_by_antid": [0] * 8,
           "period_by_antid": [100000] * 8,
       })
       input("Transmitting. Press Enter to stop... ")
       pool.wifi.stop_transmission()

The schedule arrays use hardware antenna IDs 0–7. Start times and periods
are expressed in microseconds on the sensor clock. This example does not
align transmissions across sensors or boards.

``stop_transmission()`` disables the WiFi TX schedules and leaves reception
running. Closing the pool also stops transmission. These controls are
separate from ``pool.tx``, which transmits custom IQ waveforms in IQ TX mode.

Receive while scheduling transmissions
---------------------------------------

We register CSI callbacks on the same ``pool.wifi`` object and process them
with ``pool.run()`` or ``pool.start_processing()``. See :doc:`wifi-reception`
for callbacks, calibration and backlogs.

The transceiver associates received packets with sensor TX reports. This
allows applications to compare receive and transmit timestamps using their
clock calibration. When processing packets from sensors in the pool, choose
a callback predicate that accepts the expected receiving antennas rather
than requiring CSI from the transmitting sensor too.

Schedules across boards
-------------------------

A dictionary passed to ``set_tx_config()`` applies the same configuration to
every board. For different schedules per board, pass an
``espargos.radar.RadarPoolConfig``. ``espargos.radar.build_pool_config()``
converts a schedule expressed in common calibrated time into individual
sensor clock times. This requires the corresponding WiFi clock calibration
and, for coherent operation across boards, a shared reference.

``get_tx_configs()`` returns each board's configuration. ``get_tx_config()``
returns a single configuration only when the boards agree. For direct
control of one board, use ``board.wifi_tx``.
