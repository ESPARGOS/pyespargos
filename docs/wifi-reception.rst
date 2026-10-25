WiFi Reception
==============

ESPARGOS measures channel state information (CSI) from received WiFi packets.
We collect the measurements from all selected sensors into a cluster, then
apply calibration to compare their phases.

:attr:`pool.wifi <espargos.pool.Pool.wifi>` handles both reception and transmission in the same ``wifi``
sensor mode. A sensor cannot receive while it is actively transmitting a
packet, but receives again between transmissions. Enabling a periodic TX
schedule does not disable reception for the whole schedule. See
:doc:`wifi-transmission` for sending packets.

WiFi Settings
-------------

The examples use sensors in WiFi mode and an internal reference on a single
board. In the web interface, select the WiFi channel of the transmitter and
set *Generate Phase Reference* to *During calibration*. Reference transmission
requires transmit authorization. For a shared reference across boards, see
:doc:`combined-arrays`.

Different channels within a pool
----------------------------------

We can tune subsets of sensors to different WiFi channels. For example:

.. code-block:: python

   from espargos import Pool

   with Pool("192.168.1.2") as pool:
       pool.configure(mode="wifi", channel=6, secondary_channel=0)
       pool.select(antennas=[0, 1, 2, 3]).configure(channel=1)
       calibration = pool.calibrate()

This produces separate RX calibration groups for the two channels on each board.
For boards connected to one common reference, use
:meth:`pool.calibrate(per_board=False) <espargos.pool.Pool.calibrate>` to relate their WiFi receivers across boards.
Geometry does not imply a shared reference connection.

Minimal Example
---------------

The following code example receives clustered CSI from one ESPARGOS device:

.. code-block:: python

   import espargos

   with espargos.Pool("192.168.1.2") as pool:
       pool.configure(mode="wifi")
       receiver = pool.wifi
       receiver.set_csi_acquisition_config({"acquire_csi_force_lltf": True})
       calibration = pool.calibrate(duration=2)
       states = receiver.get_calibration_states()

       def handle_csi(cluster):
           calibrated = receiver.apply_calibration(cluster, "lltf",
                                                   calibration=calibration, states=states)
           print("Got channel coefficients with shape:", calibrated.shape)

       receiver.add_callback(
           handle_csi,
           callback_predicate=lambda c: c.is_complete and c.has_lltf,
       )
       while True:
           pool.run()

:class:`Pool <espargos.pool.Pool>` manages connections and cleanup. :attr:`pool.wifi <espargos.pool.Pool.wifi>` supplies a shared
:class:`WiFiTransceiver <espargos.wifi_transceiver.WiFiTransceiver>` for packet processing. For antenna selections, combined arrays and
mixed RX/TX operation, see :doc:`pool`.

The lower-level classes remain available for custom integrations:

**The** :class:`.Board` **class** represents one ESPARGOS controller.
It connects to the controller and receives measurements from its sensors over Ethernet or USB.
WiFi reception settings and individual CSI messages are available through :attr:`~espargos.board.Board.wifi_rx`.
The optional :attr:`~espargos.board.Board.wifi_tx` interface controls scheduled WiFi transmissions.
Most CSI applications use :class:`.WiFiTransceiver`, even when working with only one ESPARGOS device.

**The** :class:`.WiFiTransceiver` **class** is responsible for handling the clustering of CSI from one or multiple ESPARGOS boards.
When the microcontrollers ("sensors") on the ESPARGOS array board receive a WiFi packet, they just forward the CSI estimates to the central controller together with packet metadata like MAC address, timestamp and frame counter.
The controller then forwards the CSI estimates to the computer running *pyespargos*, which is then responsible for figuring out which CSI estimates belong to the same WiFi packet.
This is easy to achieve by finding matching packet metadata.
By default, the CSI callback is only triggered if CSI is available from *all* sensors, but you can change this behavior (see documentation of :func:`~espargos.wifi_transceiver.WiFiTransceiver.add_callback` for details).
The example enables ``acquire_csi_force_lltf`` so that every supported OFDM packet is represented by its L-LTF channel estimate, regardless of whether its native format is legacy, HT, or HE.
This gives applications a consistent 53-subcarrier CSI representation across different transmitters.
The callback predicate additionally ignores packets without usable L-LTF CSI, such as 802.11b packets.

Obtain receivers through :attr:`pool.wifi <espargos.pool.Pool.wifi>` or :attr:`pool.iq <espargos.pool.Pool.iq>`. They share the connections
and processing lifecycle owned by the pool.
Call :meth:`pool.start_processing() <espargos.pool.Pool.start_processing>` to run callbacks in the background, or call
:meth:`pool.run() <espargos.pool.Pool.run>` regularly from your own loop. Backlogs collect data through
callbacks and do not own a worker thread.
Each call waits briefly for the first message and then processes one bounded batch of queued messages.
Use ``timeout=0`` when integrating with a GUI or another event loop that must never block; a dedicated worker may use a longer timeout such as ``timeout=0.5``.

With CSI Backlog
----------------
When working with a :class:`.WiFiTransceiver` of ESPARGOS devices, you get a callback whenever there is a new complete CSI cluster.
However, in many cases, you don't care about the instantaneous CSI at this very moment in time, but instead want to operate on the last couple of channel estimates.
This is what the :class:`.CSIBacklog` class is for:
This class collects CSI alongside other data (like timestamps, RSSI) from complete cluster up until a certain predefined size limit is reached.
The application code can query the backlog whenever it needs recent CSI.

.. code-block:: python

  import espargos
  import time

  with espargos.Pool("192.168.1.2") as pool:
      pool.configure(mode="wifi")
      receiver = pool.wifi
      receiver.set_csi_acquisition_config({"acquire_csi_force_lltf": True})
      pool.calibrate(duration=2)
      backlog = espargos.CSIBacklog(
          receiver,
          fields=["lltf"],
          size=20,
      )
      backlog.add_filter(espargos.Exclude11bFilter())
      try:
          pool.start_processing()

          # Collect a few seconds of WiFi packets.
          time.sleep(4)

          csi_lltf = backlog.get("lltf")
          print("Received CSI:", csi_lltf)
      finally:
          backlog.close()

The backlog supports multiple data fields that can be retrieved using the :meth:`~espargos.csi_backlog.CSIBacklog.get` method:

* ``lltf`` - L-LTF CSI data
* ``ht20`` - HT20 CSI data
* ``ht40`` - HT40 CSI data
* ``rssi`` - RSSI values
* ``timestamp`` - Sensor timestamps
* ``host_timestamp`` - Host timestamps
* ``mac`` - Source MAC addresses

You can also use :meth:`~espargos.csi_backlog.CSIBacklog.get_multiple` to retrieve multiple fields atomically.

Advanced Usage
--------------
Check out the source code of our `demo applications <https://github.com/ESPARGOS/pyespargos/tree/main/demos>`_ to learn how to use *pyespargos* in a real-time application.
