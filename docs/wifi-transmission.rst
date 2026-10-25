WiFi Transmission
===================

.. warning::

   **Authorization to transmit is required.**

   Enable transmit authorization in the controller's Settings only after
   confirming that you are legally permitted to transmit with the selected
   frequency, bandwidth, power and antenna configuration.

ESPARGOS sensors can send WiFi packets on a repeating schedule. We use
:attr:`pool.wifi <espargos.pool.Pool.wifi>` for both transmission and CSI reception: both operate in the
same ``wifi`` sensor mode. There is no separate WiFi TX mode.

A sensor cannot receive while actively transmitting a packet. Between
transmissions it resumes reception, even while its TX schedule remains
enabled. Other sensors can receive those packets and measure their CSI.
A transmitting sensor does not receive its own packet, so that packet’s CSI
cluster will be missing that sensor’s measurement.

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

:meth:`stop_transmission() <espargos.wifi_transceiver.WiFiTransceiver.stop_transmission>` disables the WiFi TX schedules and leaves reception
running. Closing the pool also stops transmission. These controls are
separate from :attr:`pool.tx <espargos.pool.Pool.tx>`, which transmits custom IQ waveforms in IQ TX mode.

Receive while scheduling transmissions
---------------------------------------

We register CSI callbacks on the same :attr:`pool.wifi <espargos.pool.Pool.wifi>` object and process them
with :meth:`pool.run() <espargos.pool.Pool.run>` or :meth:`pool.start_processing() <espargos.pool.Pool.start_processing>`. See :doc:`wifi-reception`
for callbacks, calibration and backlogs.

The transceiver associates received packets with sensor TX reports. This
allows applications to compare receive and transmit timestamps using their
clock calibration. When processing packets from sensors in the pool, choose
a callback predicate that accepts the expected receiving antennas rather
than requiring CSI from the transmitting sensor too.

Schedules across boards
-------------------------

A dictionary passed to :meth:`set_tx_config() <espargos.wifi_transceiver.WiFiTransceiver.set_tx_config>` applies the same configuration to
every board. For different schedules per board, pass an
:class:`espargos.wifi_tx.WiFiTxConfig <espargos.wifi_tx.WiFiTxConfig>`. :func:`espargos.wifi_tx.build_tx_config() <espargos.wifi_tx.build_tx_config>`
converts a schedule expressed in common calibrated time into individual
sensor clock times. This requires the corresponding WiFi clock calibration
and, for coherent operation across boards, a shared reference.

:meth:`get_tx_configs() <espargos.wifi_transceiver.WiFiTransceiver.get_tx_configs>` returns each board's configuration. :meth:`get_tx_config() <espargos.wifi_transceiver.WiFiTransceiver.get_tx_config>`
returns a single configuration only when the boards agree. For direct
control of one board, use ``board.wifi_tx``.

Transmit reports
----------------

:meth:`board.wifi_tx.subscribe_reports(callback) <espargos.board_wifi_tx.WiFiTxCapability.subscribe_reports>` supplies decoded
:class:`WiFiTxReportPacket <espargos.wifi_tx_packet.WiFiTxReportPacket>` messages. When received CSI matches a transmission,
:attr:`CSICluster.tx_report <espargos.csi_cluster.CSICluster.tx_report>` contains the report and ``tx_index`` identifies the
transmitting sensor. ``has_tx_report`` indicates whether a report is available.
``is_scheduled_tx`` identifies CSI from scheduled ESPARGOS transmissions, even
if the transmit report has not arrived.

CSI backlogs expose ``tx_timestamp``, ``tx_index``, ``tx_power`` and
``tx_rf_switch_state``. For phase processing that needs the transmit time,
:func:`espargos.wifi_tx.correct_csi_tx_timestamps() <espargos.wifi_tx.correct_csi_tx_timestamps>` applies the timestamp correction
using the calibrated sensor clock offsets.
