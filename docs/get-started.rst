Getting Started
===============

Before you get started with *pyespargos*, make sure that your ESPARGOS controller can be reached either via its IP address or a hostname.
In the following examples, we will assume that ESPARGOS controllers are reachable at IP addresses :code:`192.168.1.2`, :code:`192.168.1.3`, :code:`192.168.1.4` and so on,
but you may replace the IP addresses with hostnames if you prefer that.

WiFi Settings
-------------

Before you can capture channel state information (CSI), you must make sure to configure suitable WiFi settings in the `ESPARGOS web interface <https://espargos.net/setup/>`_.
ESPARGOS configuration is persistent across reboots, so you only need to do this once.

Make sure that the following options are configured correctly before using *pyespargos*:

* Set *Generate Phase Reference* to *During calibration*
* Leave *Reference Signal Source* at *Internal* (the default for a standalone array). Only change this for phase-coherent multi-board setups, see :doc:`combined-arrays`.
* Select the correct WiFi country code and make sure that you are allowed to use the desired WiFi channel in your country.
* Select a suitable WiFi primary and secondary channel for your device. Make sure your settings are correct by checking if you can receive CSI from your device (i.e., while "Antenna" is selected as a source) in the *Live CSI* tab.
* You can choose a small calibration signal interval, e.g. 10 milliseconds, then calibration takes less time.

Minimal Example
---------------

The following code example receives clustered CSI from one ESPARGOS device:

.. code-block:: python

   import espargos

   # Connect to ESPARGOS board at IP address 192.168.1.2
   board = espargos.Board("192.168.1.2")

   # Create new ESPARGOS pool with only one board
   pool = espargos.Pool([board])

   # Always acquire the legacy long training field (L-LTF), independently of
   # the received WiFi packet format.
   pool.set_csi_acquire_config({"acquire_csi_force_lltf": True})

   # Start sensor-message reception for all boards in the pool
   # (just one board in this case).
   pool.start()

   try:
       # Collect CSI from the reference channel for calibration.
       pool.calibrate(duration=2)

       # Get a callback whenever CSI for one WiFi packet is available from all
       # antennas. clustered_csi is an instance of CSICluster.
       def handle_csi(clustered_csi):
           csi_raw = clustered_csi.deserialize_csi_lltf()
           csi_calibrated = pool.get_calibration().apply_lltf(csi_raw)
           print("Got channel coefficients with shape:", csi_calibrated.shape)

       def complete_lltf(clustered_csi):
           return clustered_csi.get_completion_all() and clustered_csi.has_lltf()

       pool.add_csi_callback(handle_csi, cb_predicate=complete_lltf)

       # Main loop; add your break condition here.
       while True:
           pool.run()
   finally:
       pool.stop()

The example illustrates the basic usage of the :class:`.Board` and :class:`.Pool` classes:

**The** :class:`.Board` **class** represents one ESPARGOS controller.
It handles controller configuration and receives the sensor-message stream over UDP, WebSocket, or UART.
The controller efficiently transports fragments from the sensors without interpreting their message-specific payloads; :class:`.Board` reassembles those fragments and dispatches complete messages to callbacks selected by their four-byte type header.
CSI applications normally use :class:`.Pool`, even with a single ESPARGOS device.
Extensions that define another sensor-message type may instead subscribe directly through :meth:`~espargos.board.Board.subscribe_sensor_messages` and perform their own decoding.

**The** :class:`.Pool` **class** is responsible for handling the clustering of CSI from one or multiple ESPARGOS boards.
When the microcontrollers ("sensors") on the ESPARGOS array board receive a WiFi packet, they just forward the CSI estimates to the central controller together with packet metadata like MAC address, timestamp and frame counter.
The controller then forwards the CSI estimates to the computer running *pyespargos*, which is then responsible for figuring out which CSI estimates belong to the same WiFi packet.
This is easy to achieve by finding matching packet metadata.
By default, the CSI callback is only triggered if CSI is available from *all* sensors, but you can change this behavior (see documentation of :func:`~espargos.pool.Pool.add_csi_callback` for details).
The example enables ``acquire_csi_force_lltf`` so that every supported OFDM packet is represented by its L-LTF channel estimate, regardless of whether its native format is legacy, HT, or HE.
This gives applications a consistent 53-subcarrier CSI representation across different transmitters.
The callback predicate additionally ignores packets without usable L-LTF CSI, such as 802.11b packets.

An ESPARGOS pool is initialized with a list of objects of the :class:`.Board` class, which can also contain just one entry if you only use a single ESPARGOS device.
Applications must call :meth:`~espargos.pool.Pool.run` regularly unless a helper such as :class:`.CSIBacklog` does so in its own worker thread.
Each call waits briefly for the first message and then processes one bounded batch of queued messages.
Use ``timeout=0`` when integrating with a GUI or another event loop that must never block; a dedicated worker may use a longer timeout such as ``timeout=0.5``.

With CSI Backlog
----------------
When working with a :class:`.Pool` of ESPARGOS devices, you get a callback whenever there is a new complete CSI cluster.
However, in many cases, you don't care about the instantaneous CSI at this very moment in time, but instead want to operate on the last couple of channel estimates.
This is what the :class:`.CSIBacklog` class is for:
This class collects CSI alongside other data (like timestamps, RSSI) from complete cluster up until a certain predefined size limit is reached.
The application code can query the backlog whenever it needs recent CSI.

.. code-block:: python

  import espargos
  import time

  pool = espargos.Pool([espargos.Board("192.168.1.2")])
  pool.set_csi_acquire_config({"acquire_csi_force_lltf": True})
  pool.start()
  backlog = None
  try:
      pool.calibrate(duration=2)
      backlog = espargos.CSIBacklog(
          pool,
          fields=["lltf"],
          size=20,
      )
      backlog.add_filter(espargos.Exclude11bFilter())
      backlog.start()

      # Wait for a while to collect some WiFi packets in the backlog...
      time.sleep(4)

      csi_lltf = backlog.get("lltf")
      print("Received CSI:", csi_lltf)
  finally:
      if backlog is not None:
          backlog.stop()
      pool.stop()

The backlog supports multiple data fields that can be retrieved using the :meth:`~espargos.backlog.CSIBacklog.get` method:

* ``lltf`` - L-LTF CSI data
* ``ht20`` - HT20 CSI data  
* ``ht40`` - HT40 CSI data
* ``rssi`` - RSSI values
* ``timestamp`` - Sensor timestamps
* ``host_timestamp`` - Host timestamps
* ``mac`` - Source MAC addresses

You can also use :meth:`~espargos.backlog.CSIBacklog.get_multiple` to retrieve multiple fields atomically.

Advanced Usage
--------------
Check out the source code of our `demo applications <https://github.com/ESPARGOS/pyespargos/tree/main/demos>`_ to learn how to use *pyespargos* in a real-time application.
