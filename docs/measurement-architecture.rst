Measurement architecture
========================

Measurement support has four layers. Applications can use any layer without
constructing a demo or importing Qt.

* ``Board`` owns control and transport connections. A ``BoardCapability`` wraps
  format-specific controls and subscriptions, such as ``board.ble``. A subscription
  decodes a payload and preserves the originating ``SensorMessage`` metadata.
* A packet class represents one sensor report. It preserves native samples,
  metadata and validation evidence. Decoding does not associate sensors or apply
  calibration.
* A ``SensorCluster`` represents one logical measurement across sensors. It maps
  firmware antenna IDs to ``(board, row, column)``, exposes completion and age,
  and detects conflicting reports. The measurement determines its association
  key and scope. Equal packet contents alone need not identify the same event.
* A ``ClusterAssembler`` subscribes through board capabilities, associates
  reports, retains incomplete measurements and dispatches callbacks. ``Pool``
  owns the assemblers, their processing thread and their subscription cleanup.

Processing functions consume clusters to estimate phase, decode signals or
produce spectra. They do not acquire streams or change radio settings.
Calibration acquisition belongs to the receiver, while the resulting correction
model is independent of visualization. A camera chooses measurements and renders
results. An advertiser list is application state, not receiver state.

Raw storage uses the shared ``SensorBacklog`` ring buffer and filter chain.
``CSIBacklog``, ``IQBacklog`` and ``BLEBacklog`` declare their measurement fields
and subscribe to their receiver. A backlog does not configure hardware or
perform calibration. Variable-length BLE captures remain packet objects rather
than being padded to an arbitrary common sample count. Processing windows such
as ``BLEPhaseHistory`` are separate from raw recording and can be maintained
independently for each source.

Receiver lifecycle
------------------

``pool.wifi``, ``pool.iq`` and ``pool.ble`` lazily construct receivers. This does
not select sensor modes or apply radio settings. All instantiated receivers
participate in ``pool.run()`` or the shared ``pool.start_processing()`` worker.
Callbacks execute on that processing thread and should return promptly.
``pool.close()`` detaches subscriptions and closes the shared connections.
It does not generally restore earlier receiver settings. Applications that
promise restoration must snapshot and restore the settings they change.

Completion and phase validity are separate. A complete BLE cluster can have
poor waveform coherence. A partial cluster can still identify a packet or be
useful to a recorder. Callback predicates select when a consumer receives an
event. Each callback fires at most once for each retained cluster. Early
callbacks can see a cluster that later acquires more reports, so copy any data
that needs to remain a snapshot.

Adding a measurement type
-------------------------

A future preamble-triggered WiFi IQ receiver can add a decoded packet type,
a board capability subscription, a ``SensorCluster`` subclass and a
``ClusterAssembler`` subclass. Register the receiver once:

.. code-block:: python

   Pool.register_receiver("wifi_packet_iq", WiFiPacketIQReceiver,
                          sensor_mode="wifi_packet_iq")
   receiver = pool.get_receiver("wifi_packet_iq")

This is an illustrative name, not an implemented firmware mode. The optional
``sensor_mode`` tells the pool which configured sensors the receiver expects.
The factory takes the owning pool. Registration does not add a new transport,
processing thread or shutdown path. The receiver must detach any subscriptions
if its constructor fails after partially subscribing.

Do not infer shared timing from a common Python interface. IQ sample-grid
indices, WiFi packet identities and BLE packet events have different meanings.
The existing association hooks support exact keys and disambiguation among
repeated signatures. BLE associates independently qualified packets and applies
stricter waveform validation when their CRC seed is unknown. Such rules belong
to the measurement type. They should only become common infrastructure when
another measurement type shares the same semantics.

BLE scope
---------

BLE packets currently have board-local clock epochs and timestamps. A BLE
cluster identifies its originating ``board_index`` and uses the pool's array
shape, leaving other board positions empty. Its ``is_complete`` refers to all
eight sensors on that board. Receiver callback completion can instead follow
the active BLE sensor mask. Multiple boards can be monitored independently.
No cross-board event association or phase coherence is implied.

Configuration and calibration ownership
---------------------------------------

``Pool.configure`` delegates to mode-specific planners registered with
``Pool.register_configuration(mode, planner)``. A planner receives a
``ConfigurationContext`` and the non-None requested options, validates them,
and returns a ``ConfigurationUpdate``. Planning may read current hardware state.
It must defer writes to the update's actions. Every board is planned before any
write begins. This is validation before application, not atomic hardware rollback.

Receiver registration and mode configuration are separate. A new measurement
within an existing sensor mode only needs its receiver/capability. A new firmware
mode can additionally register a planner without adding branches to Pool.
The firmware must independently support that mode.

Pool reports attempted mode transitions through ``receiver.modes_changed``.
Receivers own their calibration validity and configuration-related invalidation.
BLE gain and input changes therefore preserve calibration through both
``pool.configure`` and ``pool.ble.apply_config``. Failed writes invalidate the
relevant correction conservatively. WiFi channel writes likewise go through the
receiver when it exists, including scoped updates.

Buffering and overload
-----------------------

Each assembler exposes ``configure_buffering(BufferPolicy(...))``,
``buffer_policy`` and ``buffer_stats``. Defaults retain at most 4096 reports,
drop the oldest on overflow, and process up to 256 reports per receiver turn.
This bounds pending memory and gives other receivers processing opportunities.
Callbacks should still return promptly because a batch limit cannot preempt
application code or expensive association work.

The BLE camera uses a 256-report limit, a 0.5-second maximum arrival age and
64-report batches. Stale input is expired at dequeue as well as enqueue.
For recording, choose an appropriate capacity and inspect ``dropped`` and
``expired`` counters. ``overflow="newest"`` preserves already queued reports.
``capacity=None`` explicitly allows an unbounded pending queue. This does not
make transport or hardware acquisition lossless. These limits are independent
of cluster caches and the raw recording backlog.

Reusable processing and demo controls
--------------------------------------

``espargos.BLEBeamformer`` consumes arrays and a ``BLEBeamformingConfig``.
It returns ``BLEBeamformingResult`` with a grid spectrum or fitted direction
cosines and strengths, plus Jones vectors when available. Numerical processing
has no camera, Qt, marker or display-mode dependency. Each source has its own
phase history. An injectable clock supports offline replay in the observation
time base. The camera alone draws fitted paths as finite-width spots.

``demos.ble.common.BLEController`` (in ``controller.py``) runs configuration and
calibration asynchronously and exposes state for ``BLERxDrawer``. It accepts a
pool factory, supports a board index and restores the settings it changes when
closed. Attaching to an already processing pool leaves that processing running.
The controller does not close its pool. ``Advertisers`` in the same common
directory is a reusable Qt list model, separate from acquisition and beamforming.
