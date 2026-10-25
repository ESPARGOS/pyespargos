Boards and pools
=====================

:class:`Board <espargos.board.Board>` represents one ESPARGOS array. :class:`Pool <espargos.pool.Pool>` groups one or more boards
and owns their connections. The boards need not form a regular arrangement.
Use it as a context manager so that streams are closed and transmissions
stopped when your program leaves the block:

.. code-block:: python

   from espargos import Pool

   with Pool("192.168.1.2") as pool:
       pool.configure(mode="iq_rx", frequency_hz=2437e6)
       calibration = pool.calibrate()

You can also pass existing boards. The pool uses those exact objects and closes
them when it exits::

   from espargos import Board, Pool

   with Pool([Board("192.168.1.2"), Board("172.16.0.241")]) as pool:
       pool.configure(mode="wifi", channel=6)

No geometry is needed for reception or explicit per-antenna TX weights.
For an aperture kit, load the existing configuration file instead:

.. code-block:: python

   pool = Pool.from_config("config/aperture-kit-8x4.yml")

The file supplies board names, addresses, antenna geometry and reference-cable
information. Loading it does not connect to hardware, change modes or enable
transmission. Entering ``with pool:`` opens the connections. For a GUI, call
:meth:`pool.open() <espargos.pool.Pool.open>` and :meth:`pool.close() <espargos.pool.Pool.close>` explicitly from its hardware worker.
A pool cannot be reopened after closing it.

Choose antennas, then configure them
------------------------------------

A selection is a view of the same hardware. It neither opens new connections
nor owns a separate lifetime. Select boards by object, address, or the names
from the configuration file:

.. code-block:: python

   with Pool.from_config("config/aperture-kit-8x4.yml") as pool:
       transmitters = pool.select(boards=["ba1", "ba2"])
       receivers = pool.select(boards=["ba3", "ba4"])

       transmitters.configure(mode="iq_tx", frequency_hz=2437e6,
                              rf_gain={"a": 0, "b": 0})
       receivers.configure(mode="iq_rx", frequency_hz=2437e6,
                           sample_rate_hz=80e6, rx_gain=30)

       # Prepare TX waveforms before requesting TX calibration.
       # See IQ Transmission for waveform and beam controls.

Choose individual antennas with hardware IDs 0–7, rather than row/column
indices. IDs apply to each board in the selection:

.. code-block:: python

   selected = pool.select(boards="ba1", antennas=[0, 2, 5])
   selected.configure(mode="iq_rx", frequency_hz=2412e6)

Nested selections can only narrow the selected set. For different antenna
lists on different boards, pass explicit :class:`Antenna(host, antid) <espargos.calibration.Antenna>` identities.

:meth:`configure() <espargos.pool.AntennaSelection.configure>` changes the supplied fields on selected antennas. It does not
calibrate. Antennas entering IQ TX remain silent even if a waveform was
previously loaded. RX sample rate and RX port routing are board-wide hardware
settings, so changing them requires selecting all eight antennas on that board.
TX gain, bandwidth, frequency and output port can be set per antenna.

For per-sensor WiFi channels and their calibration, see :doc:`wifi-reception`.

Receive and transmit
---------------------

:attr:`pool.iq <espargos.pool.Pool.iq>` and :attr:`pool.wifi <espargos.pool.Pool.wifi>` provide shared :class:`IQReceiver <espargos.iq_receiver.IQReceiver>` and :class:`WiFiTransceiver <espargos.wifi_transceiver.WiFiTransceiver>` objects.
Use their callbacks and backlogs, with :class:`Pool <espargos.pool.Pool>` driving processing, as described in
:doc:`iq-reception` and :doc:`wifi-reception`. Both use the same board connections,
and :class:`Pool <espargos.pool.Pool>` manages their lifetime. Default callbacks wait for antennas in the
corresponding mode. Other antennas remain missing in the returned data. A
selection does not change the shape of receiver data.

WiFi packet transmission also uses :attr:`pool.wifi <espargos.pool.Pool.wifi>`, as described in
:doc:`wifi-transmission`.

:attr:`pool.tx <espargos.pool.Pool.tx>` provides waveform and beam controls, described in
:doc:`iq-transmission`. It also shares the pool's connections and IQ receiver.
Beamspace steering across boards requires geometry from :meth:`from_config() <espargos.pool.Pool.from_config>`.
A :class:`Pool <espargos.pool.Pool>` built from addresses alone can still use explicit per-antenna
phases and delays.
Creating these TX controls mutes transmission. Preparing a waveform does not
start it. Use :meth:`selection.start_transmission() <espargos.pool.AntennaSelection.start_transmission>` and
:meth:`selection.stop_transmission() <espargos.pool.AntennaSelection.stop_transmission>` to control selected transmitters. Stop sends
silence while replay continues, preserving calibration.

These calls are blocking. GUI applications should serialize hardware operations
on a worker thread, as the demos do. Do not close individual boards or shared
receivers while other parts of your application are using the pool.

Calibrate the configured roles
------------------------------

:meth:`pool.calibrate() <espargos.pool.Pool.calibrate>` examines the current modes. It calibrates reception for
WiFi/IQ RX antennas and transmission for IQ TX antennas, returning a
:class:`Calibration <espargos.calibration.Calibration>`. :meth:`selection.calibrate() <espargos.pool.AntennaSelection.calibrate>` returns only the requested antenna
coverage. Recalibration must include each overlapping calibration group in
full. Using a subset without recalibrating is always allowed.
Successful results are also merged into :attr:`pool.calibrations <espargos.pool.Pool.calibrations>`.

TX uses reciprocal sensor measurements by default. Select
``tx_method="monitor"`` for a bidirectional REF path to the controller, or
``path="antenna_r"`` / ``"antenna_l"`` for OTA reciprocal measurements.

IQ calibration requires a common frequency and RX sample rate within the
selection. Sensors outside it keep their modes, tuning and stored corrections.
Shared capture controls and transmission pause during the measurement.
Subgroup recalibration reuses an existing sample grid at 80 MSa/s. See
:doc:`calibration` for initial synchronization and independent group references.
WiFi calibration can measure multiple channel groups without retuning receivers.

Saved calibration is bound to the hardware state in which it was measured.
Changing modes or retuning can invalidate it. See :doc:`calibration` for group
references, subset selection and validity checks.

Connection ownership
--------------------

Only :class:`Pool <espargos.pool.Pool>` starts, reconnects and closes board streams. Its CSI and IQ
receivers assemble incoming messages. :meth:`pool.start_processing() <espargos.pool.Pool.start_processing>` runs their
callbacks on one shared worker, including receivers first accessed after the
worker starts. :meth:`pool.stop_processing() <espargos.pool.Pool.stop_processing>` stops that worker without closing
connections. Receivers have no independent start, stop or close operation.
Backlog ``close()`` only removes its callback. Calibration results live in
:attr:`pool.calibrations <espargos.pool.Pool.calibrations>`.

CSI getters that return one pool-wide value raise ``ValueError`` when boards
disagree. They never change settings. Read individual :class:`Board <espargos.board.Board>` capabilities
when different settings are intentional, or explicitly set a common value.

Mixed roles
-----------

For an aperture kit with sensor 1 on each board receiving WiFi and the other
sensors receiving IQ:

.. code-block:: python

   with Pool.from_config("config/aperture-kit-8x4.yml") as pool:
       pool.configure(mode="iq_rx", frequency_hz=2437e6, sample_rate_hz=80e6)
       pool.select(antennas=[1]).configure(mode="wifi", channel=6)

       pool.iq.add_callback(handle_iq)
       pool.wifi.add_callback(handle_csi)
       pool.start_processing()
       input("Press Enter to stop\n")

This uses four boards, one pool, a WiFi transceiver and an IQ receiver. Calibration inspects the
current modes and requests the appropriate IQ receive, WiFi receive, and IQ
transmit measurements for the selection.

The controller reference radio operates independently of sensor modes, so the
same kit can calibrate both groups. Inside the ``with`` block:

.. code-block:: python

   calibration = pool.calibrate(per_board=False)

The result contains a WiFi receive group for the four selected WiFi sensors
and an IQ receive group for the other 28 sensors.

Selections do not reserve antennas or remember their roles. If selections
intersect, a later :meth:`configure(mode=...) <espargos.pool.AntennaSelection.configure>` changes the overlapping antennas to
that mode. Each antenna has exactly one operating mode at a time. Mixing roles
in a calibration request is valid. Unsupported combinations, such as IQ
calibration across different center frequencies, are rejected before acquisition.

Controller reference radio
--------------------------

Reference generation is independent of sensor modes. A controller can generate WiFi reference packets while its sensors
receive IQ, or generate wideband noise while its sensors use WiFi. Sensor mode
changes do not stop or retune the controller reference.

The controller has one radio. CW/noise and reference monitoring cannot run at
the same time. Stop the active function before selecting the other. WiFi
reference packets pause automatically while either function owns the radio,
then resume according to the configured reference policy. WiFi reference
configuration changes are rejected while CW/noise or monitoring is active.
Calibration routines manage these transitions for their measurements.

We can read and select the controller mode with ``get_controller_mode`` and
``set_controller_mode`` (``{"mode": "wifi"}`` or ``{"mode": "iq"}``).
This runtime selection leaves sensor modes unchanged. Selecting WiFi stops
CW/noise and monitoring and restores the configured WiFi reference policy.
Selecting IQ suppresses WiFi reference packets and restores the configured
CW/noise signal, if one was enabled before switching to WiFi. An explicit
reference-off request clears that selection.
The web interface's global mode buttons set both controller and sensor modes.
