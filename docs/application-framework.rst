The Common Demo Application Framework
=====================================

The WiFi and WiFi IQ cameras show the same scene from two different
measurements. The WiFi camera reads CSI from the modem. The WiFi IQ camera
estimates CSI from the packet waveform. Each has its own receiver controls,
but both feed a CSI backlog into the same beamformer, camera overlay and QML
window. When writing a new demo, start with the application that supplies
your measurement. You can reuse its controls even if your plot looks nothing
like a camera.

The framework lives in the checkout under ``demos/``. For a recorder or
another application without a Qt interface, use :class:`.Pool` directly.

Choose a starting point
-----------------------

.. list-table::
   :header-rows: 1
   :widths: 27 43 30

   * - Measurement
     - Application code
     - Example in ``demos/``
   * - Modem WiFi CSI
     - ``ESPARGOSCSIApplication`` in ``wifi/common/application.py``
     - ``wifi/instantaneous-csi``
   * - CSI from WiFi packet IQ
     - ``WiFiIQApplication`` in ``wifi_iq/common/application.py``
     - ``wifi_iq/camera``, ``wifi_iq/instantaneous-csi``
   * - BLE packet IQ
     - ``BLEMeasurementApplication`` in ``ble/common/application.py``
     - ``ble/phases-by-antenna``
   * - Raw IQ
     - ``ESPARGOSIQApplication`` in ``iq/common/application.py``
     - ``iq/signal-analyzer``
   * - IQ transmission
     - ``ESPARGOSTxApplication`` in ``iq/transmit-beamforming/application.py``
     - ``iq/transmit-beamforming``

Packet backlog controls
-----------------------

BLE, WiFi and WiFi IQ packet demos place **Packet backlog** at the top of
the App drawer. Use **Packets** to set the window size. Demos with an age
cutoff or a packet admission threshold also show **Max age** or **Min. antennas**.
WiFi storage and format filters appear below these controls.

Use ``common/BacklogWindowSettings.qml`` to bind these controls to a demo's
existing settings. CSI demos use ``wifi/common/CSIBacklogSettings.qml``,
which binds the packet count and storage filters to the CSI backlog.

A pool for the whole application
--------------------------------

``ESPARGOSApplication`` reads the configuration and owns the Qt window and
shutdown hooks. Receive demos call ``initialize_pool()`` to open the pool and
set up their receiver. The receiver, settings drawer and backlog use the same
board connections. When a demo needs antenna positions, ``CombinedArrayMixin``
loads the layout and cable lengths. Pass ``-c FILE`` for an assembled array or
``-s HOST`` for one board in its standard orientation.

A controller can supply the reference without contributing any antennas.
In a layout file, list the sensing boards under ``combined-array.boards``
and give the master's address as ``combined-array.reference_controller``.
For a demo that takes host addresses, supply the master separately. For
example, this command measures CSI on two boards using a shared reference::

   python demos/wifi/instantaneous-csi/main.py 192.168.1.3,192.168.1.4 \
       -o pool.reference_controller=192.168.1.2 -o pool.calibration.per_board=false

The cameras and drawers count measurement boards. The WiFi IQ camera accepts
one receiving board, which can have a separate reference controller. See
:doc:`modes` for the SDK equivalent and :doc:`combined-arrays` for wiring.

CSI plots and cameras
---------------------

Both WiFi applications derive from ``CSIApplication``. Add ``CSIBacklogMixin``
to keep recent packets in ``self.backlog`` and ``SingleCSIFormatMixin`` to
read a training field with ``get_backlog_csi()``. Put the mixins before the
receiver application in the inheritance list. The WiFi IQ receiver currently
supplies the legacy long training field (L-LTF). The modem receiver also
offers HT and HE formats.

The two camera entry points are ``wifi/camera/main.py`` and
``wifi_iq/camera/main.py``. To change how both display CSI, edit
``wifi/camera/backend.py`` or ``wifi/camera/CSIWindow.qml``. To change WiFi IQ
acquisition or calibration, edit ``wifi_iq/common/``. Its drawer controls the
channel, automatic or manual gain, antenna feed and shared trigger. Selecting a transmitter
by MAC filters the backlog. Both instantaneous CSI demos share the plot code
in ``wifi/common/instantaneous_csi.py`` and the same chart window.

For example, run the WiFi IQ camera on channel 5 with the video input disabled::

   python demos/wifi_iq/camera/main.py -s 192.168.1.3 --no-camera \
       -o wifi_iq.channel=5 -o wifi_iq.rx_gain=30

All four radio cameras use the video and overlay components in
``demos/common/camera``. Their receiver-specific calculations stay with the
corresponding WiFi, BLE or IQ demo.

Drawers and workers
-------------------

The shared QML window accepts ``receiverDrawerComponent`` on the left and
``appDrawerComponent`` on the right. An omitted drawer has no button.
``initialize_qml()`` exposes the application as ``backend`` and the settings
as ``appconfig`` and ``genericconfig``. Pass additional objects in
``context_props``. For example, both CSI cameras use the same window and each
assigns a different receiver drawer in its own ``Main.qml``.

* **BLE.** ``BLEController`` handles tuning, reference acquisition and restoring
  receiver settings. ``BLEMeasurementApplication`` provides advertiser
  selection and a ``process_cluster()`` hook. Slow display processing runs on a
  worker that keeps the newest pending measurement per advertiser. The BLE
  camera also uses this controller and adds its own beamforming and video.
* **Raw IQ.** ``ESPARGOSIQApplication`` starts the receiver's processing worker.
  Use ``create_iq_controller()`` for a drawer that controls capture and
  synchronization. The signal analyzer uses the same time and spectrum displays
  for triggered IQ and :doc:`iq-receive-streaming`.
* **IQ transmission.** The beamforming demo exposes its worker as ``txcontrol``.
  It connects after QML loads, prepares the sensors, calibrates and starts
  playback. The window calls ``txcontrol.close()`` so the worker can mute the
  transmitters before Qt exits. The default calibration method is monitor for
  a standalone board and reciprocity for multiple boards or a dedicated
  master. Use ``--tx-calibration`` to choose the method for your wiring.

Configuration
-------------

Use ``DEFAULT_CONFIG`` for a demo's ``app`` settings. The framework merges
defaults with the YAML file passed via ``-c``, then applies each dotted
``-o KEY=VALUE`` override. Values use YAML types. Run a demo with ``--help`` to see its settings and defaults.

For instance, to keep a longer history in a native CSI demo, use
``-o backlog.size=64``. To turn off video, pass
``-o app.camera.enable=false`` or use the equivalent YAML::

   app:
     camera:
       enable: false
