BLE reception
=============

``board.ble`` provides decoded packet-IQ subscriptions and live receiver
configuration. ``pool.ble`` assembles sensor captures into ``BLECluster``
objects using the common pool processing and callback infrastructure.
Capture supports LE1M and LE2M on the selected channel without following channel
hopping. Supported capture rates are 4, 8 and 16 MSa/s. Auto uses 4 MSa/s for LE1M and 8 MSa/s for LE2M. At higher sample rates,
the fixed 16,384-sample validation history limits the maximum packet duration.
Neither API depends on the camera demo. See :doc:`measurement-architecture`.

Auto is recommended for phase measurements. In hardware testing, 16 MSa/s
retained consistent phase but yielded fewer complete eight-sensor events than
4 or 8 MSa/s. Short raw captures remain valid packet reports, but phase fitting
also needs sufficient usable waveform after filtering and the detector tail.
At 16 MSa/s, 1,024 samples were too short for the reference phase estimator.

Discover and record packet events
---------------------------------

.. code-block:: python

   from espargos import Pool

   def discover(cluster):
       packet = cluster.packet
       if packet.is_advertisement and not cluster.is_calibration:
           print(packet.advertiser, packet.name, packet.rx_gain)

   def record(cluster):
       for antenna_id in range(8):
           packet = cluster.get_packet(cluster.board_index, antenna_id)
           if packet is not None:
               save_capture(packet.samples, bytes(packet))  # Application-defined.

   with Pool("192.168.0.234") as pool:
       pool.configure(mode="ble", channel=37, output="antenna_r")
       receiver = pool.ble
       receiver.apply_config({"phy": "LE1M"})
       receiver.add_callback(discover, callback_predicate=lambda c: True)
       receiver.add_callback(record, include_partial=True)
       pool.start_processing()
       input("Receiving BLE. Press Enter to stop... ")

Discovery runs on the first qualified report. Advertiser identity additionally
requires a verified advertising CRC. Recording receives complete
events immediately and incomplete events once their settle timeout expires.
The callbacks have independent delivery state. These examples leave configured
sensor modes in place on exit. The camera demo explicitly restores its previous
settings.

Use ``board.ble.subscribe_packets(callback)`` for every decoded sensor report,
including packets whose connection CRC seed is unknown. Its callback receives a
``SensorMessage[BLEPacket]`` on the board's dispatch thread. Detach it using
``board.ble.unsubscribe(handle)``. This path is useful for raw recording and
custom decoding that does not need event association.

``BLEBacklog`` follows the same storage and filter API as ``CSIBacklog`` and
``IQBacklog``. It stores raw measurements without computing phase or changing
receiver configuration. For example, attach a bounded recorder before starting
pool processing::

   from espargos import BLEBacklog

   backlog = BLEBacklog(pool.ble, fields=["packets", "rx_gain", "completion"],
                        include_partial=True, size=100)
   # Later, read a chronological snapshot using backlog.get("packets").
   # Each cell contains a BLEPacket, or None for a missing sensor.
   backlog.close()  # Detach only this consumer.

Captures and association
------------------------

A cluster retains variable-length IQ and per-sensor CRC verification, gain and
capture timing metadata. ``cluster.completion`` has shape ``(boards, 2, 4)``.
The ``packet`` property is a qualified decoded packet, not a synthesized PDU.
``packet.qualified`` and ``packet.crc_valid`` distinguish detection from verified
CRC. Advertising uses the known CRC seed. Other access addresses are detected
without a configured seed and retain an unverified received CRC. No packet
context, access-address filter or CRC seed is configured by the caller.

A repeated PDU prefix is not a unique event identifier. Association checks the
received CRC, board, clock epoch, receiver configuration, timing and sensor
occupancy. Ambiguity is rejected. Event caches are bounded and expired.
``dropped`` counts discarded association inputs and cache evictions, not network
loss. Phase processing additionally checks waveform coherence and uses stricter
validation when a packet's CRC could not be verified.

BLE events are currently board-local. A multi-board pool delivers independent
clusters with their originating ``board_index``. A cluster's ``is_complete``
means all eight sensors on that board, while the receiver's default callback
uses the current BLE sensor selection. Recreate the pool after a controller
reboot to establish a new generation history.

Phase processing and calibration
--------------------------------

``espargos.ble_processing.estimate_phase(cluster)`` returns uncalibrated
``BLEPhase`` arrays: ``vector``, ``coherence`` and ``valid``. They have the same
array shape as the cluster. Invalid positions contain NaN. Capture completion
and usable phase are separate masks. The estimator supports LE1M and LE2M.
LE Coded is unsupported.

For corrected phase, configure a reference signal on the selected channel,
start pool processing, then call ``pool.ble.calibrate(board_index=0)``.
This requires all eight sensors on that board in BLE mode. The call briefly
selects REF and manual gain at the configured gain index. It restores the
previous route and gain mode, including on acquisition failure.
It neither enables a transmitter nor changes persistent reference settings.
When using the controller's generator, select the BLE controller role and
During Calibration or Always in its reference settings. An external reference
can also supply the required advertisements. Pass its advertiser MAC address
as ``reference_address="aa:bb:cc:dd:ee:ff"`` to ``calibrate()``. By default,
calibration uses the internal controller's reference address. Unrelated traffic
is excluded even when it is captured on REF through RF leakage.

``pool.ble.phase(cluster)`` applies a matching calibration or raises
``BLECalibrationError``. The common ``SensorCalibration`` model corrects phase
and residual timing slope, including PCB reference-path delays. This correction
is measured over the occupied BLE band, not a broadband timing calibration.
RF-input, ordinary gain, sample rate, capture length changes
through ``apply_config`` preserve corrections. Other configuration changes or
unexpected receiver epochs invalidate them. Current firmware reports measured packet gain, allowing phase
and amplitude correction under AGC. Gain changes do not require recalibration.
Observations with an unstable gain window are excluded from calibrated phase.
Raw cluster callbacks continue without calibration.
For hardware changes made outside this pool, call
``receiver.invalidate_calibration()`` before using further corrected results.

Calibration results are not installed into the pool's broadband IQ/WiFi
calibration groups. Use ``pool.ble.calibrate()`` explicitly. This avoids implying
that a narrowband BLE reference establishes IQ sample timing or cross-board
coherence.

Random polarization
-------------------

With current firmware, ``rf_switch=4`` chooses an antenna input independently
after each qualified packet. Noise candidates retain the feed. The BLQ5
metadata byte at offset 94 reports the
effective route, while the common settings word retains requested mode 4.
``BLEPacket.rf_switch_mode`` is the requested mode and ``rf_switch`` is the
actual input. ``BLECluster.rf_switch_state`` exposes the actual inputs in array
order. Consumers must not coherently combine orthogonal antenna polarizations
without accounting for their different signal phases.

BLE phase history
-----------------

``espargos.BLEPhaseHistory(size=16, max_age=0.25)`` stores a bounded history for
one source. Append calibrated phasors and effective RF inputs using
``backlog.append(phase.vector[board], cluster.rf_switch_state[board])``.
The caller owns source selection, so this class has no advertiser UI or camera
dependency. Use a separate instance for each source and clear it after calibration
or receiver context changes.

``covariance()`` returns the spatial covariance after expiring old observations,
or ``None`` when some sensor pairs have not been observed. Only measurements
within the same polarization are paired. This prevents an unknown phase offset
between antenna feeds from contaminating random-polarization histories.
The result can be passed to the common MUSIC and Root-MUSIC functions.

Polarization observations
------------------------

``BLEPhase.amplitude`` retains per-sensor RMS waveform amplitudes normalized
to gain index zero when gain is known. Combine it
with calibrated unit phasors as ``phase.vector * phase.amplitude`` when relative
feed amplitudes are needed. The amplitude uses gain-normalized ADC units, not absolute field strength.

``BLEPhaseHistory.coherent_feeds()`` reconstructs a sensor-by-feed complex field from
mixed-feed packets, up to a common complex scale. It requires connected overlap
and both feeds at every sensor within the short observation window. It returns
``None`` for missing/disconnected coverage or an inconsistent channel. This
allows callers to apply ``polarization.inverse_jones_response`` independently
of a demo. Merely alternating whole-array feeds leaves their relative phase
unobservable and is not accepted.

Synthetic validation
--------------------

Run ``PYTHONPATH=. python tools/benchmark_ble_processing.py`` from the repository
root for a seeded polarization and coherent-multipath sweep. It reports rejected
polarization windows separately from errors in accepted estimates. The multipath
cases vary path separation and echo strength. These are algorithm tests, not
real-world accuracy guarantees or a substitute for reference calibration.

Packet gain metadata
--------------------

``BLEPacket.rx_gain`` reports the measured payload gain, or ``None`` when it
could not be established. ``BLECluster.rx_gain`` maps it into array order, using
NaN for missing values. ``BLEBacklog`` can retain this field. For manual captures the configured fixed gain remains a usable fallback.
AGC phase requires a measured gain. No gain is inferred from RSSI.

In BLQ5 modem flags at byte 88, bit 3 indicates gain reporting support and bit 2
indicates a stable measured gain. Bits 8–15 carry its index. The firmware reads
ADC debug byte 12 in the otherwise unused upper sample byte. The lower 20 IQ
bits and packed transport stay unchanged. A stable payload window 40–24 µs
before capture completion is required. The shared gain-phase table compensates
analog gain-state phase changes, and amplitudes are divided by the reported
1 dB-per-index gain factor.

Library beamforming
-------------------

Other applications can use the camera's numerical processing without importing
any demo code:

.. code-block:: python

   from espargos import BLEBeamformer, BLEBeamformingConfig
   from espargos.array_processing import steering_vectors_2d

   processor = BLEBeamformer()
   config = BLEBeamformingConfig(method="Root-MUSIC", polarization=True)
   steering = steering_vectors_2d(2, 4, 128, 96, frequency_hz=2402e6,
                                 pixel_centers=True)
   iq = pool.ble.packet_iq(cluster)
   phase = iq.phase()
   covariance, counts = iq.covariance()
   result = processor.process(
       source_id, phase.vector[0], cluster.rf_switch_state[0], arrival_time,
       steering, 2402e6, config, amplitude=phase.amplitude[0],
       orientations=board_orientations, covariance=covariance, sample_counts=counts)

Use the actual packet channel frequency and originating board's array slice.
Supply its antenna orientations in the same row/column order.
``arrival_time`` is host monotonic time. Require valid phase for the sensors
used by the estimator. Bartlett and MUSIC supply ``result.spectrum``.
Root-MUSIC supplies ``result.directions`` and ``result.strengths`` without
converting paths into display pixels. ``result.jones`` is optional.
``result.path_jones`` supplies independently fitted H/V vectors for resolved
MUSIC and Root-MUSIC paths. Each path can have a different arbitrary polarization. Applications
choose how to display or retain these results. Processing is single-worker,
while raw reception and backlogs retain their existing shared lifecycle.


Packet timing, IF and frequency response
---------------------------------------

``pool.ble.packet_iq(cluster)`` is the common calibrated processing path used
by the camera and phase API. It retains the whole usable captured packet,
excluding filter transients and samples outside the packet. Firmware capture
buffers can truncate a long packet. Missing samples are never synthesized.

Each sensor's capture is aligned using integer and fractional waveform delay.
If ``y[n+d]`` matches the reference ``x[n]``, mixing at independent capture
origins contributes an extra phase. Before conjugating native taps to the common
receive convention, the alignment multiplies by
``exp(+2j*pi*IF*d/sample_rate)``. Correcting the waveform shift without this IF
term gives the wrong spatial phase. A windowed-sinc interpolator avoids the
fractional-delay bias of the former cubic interpolation.

Packet timing and fixed receiver calibration are separate. BLE event timestamps
are not sample-origin timestamps, so the WiFi timestamp-based STO estimator
cannot be applied directly. Reference acquisition measures residual phase and
frequency slope after waveform alignment and passes them to the shared
``SensorCalibration`` model. The model also removes the known PCB reference
network's propagation at the absolute RF frequency. REF acquisition temporarily
uses manual gain and restores the previous gain mode and RF input.

``iq.covariance()`` returns spatial second moments and actual overlapping sample
counts. The beamformer accumulates these across its packet history for Bartlett,
MUSIC and Root-MUSIC. It does not coherently average unrelated packet phases.
Polarization reconstruction supplies a feed phase relation while retaining the
measured full-sample covariances. Phase-only observations remain supported for
applications that do not retain IQ.

``pool.ble.channel_response(cluster)`` estimates a cross-spectral channel ratio
against a received antenna waveform. No transmitted pilot is needed. It returns
``BLEChannelResponse`` with RF frequency offsets, complex response, coherence,
and sample counts. ``group_delay()`` fits local phase slopes without unwrapping
across unsupported frequency bins. These are relative responses over the BLE
band, not OFDM CSI or absolute propagation delays. Low spectral power and poor
coherence limit meaningful delay estimates over this narrow bandwidth.

The BLE instantaneous-CSI demo in ``demos/ble/instantaneous-csi`` plots power
and phase versus frequency in the same layout as the WiFi instantaneous-CSI demo. Turning off capture-timing correction restores the
estimated capture delay slope and IF phase for diagnosis. It does not turn BLE
completion timestamps into sample-origin timestamps.

The gain phase table is shared with WiFi and is an approximation. A live BLE
reference check found up to 0.48 rad residual phase change when switching from
manual gain 40 to 50 on one sensor. Switching from manual gain 40 to Auto stayed
within 0.10 rad in that test. The timing correction does not remove this
analog gain-state discrepancy. Reference delay estimates also remain noisy over
BLE's narrow bandwidth and should not be interpreted as precision ranging.

Calibration after settings changes
---------------------------------

Use ``pool.ble.apply_config()`` to preserve a valid correction when changing
RF input, manual/automatic gain, capture length or decimation. These changes leave the oscillator
held. Packet alignment handles the capture origin and uses the actual sample
rate for IF correction. More samples can improve precision, but do not establish
a new phase reference.

Channel changes and receiver/clock restarts require calibration. PHY,
expert gain-word changes and entering/leaving expert gain mode currently
invalidate it conservatively.
PHY changes alter the processed bandwidth, and expert words can alter the analog response. Reusing the narrowband
reference across those changes has not been validated. Ordinary gain changes
retain calibration, though gain-dependent analog phase errors can remain.

Preservation updates the configuration generation and still checks each sensor's
hardware epochs. A failed live reconfiguration that restarts the receiver cannot
silently retain an obsolete correction. Direct ``board.ble.set_config()`` calls
do not perform this managed preservation.

The BLE ``phases-by-antenna`` demo displays the same full-packet phase estimator
in the physical antenna layout, including each sensor's effective RF feed.

Host decoding
-------------

BLE always streams full IQ candidates and decodes them on the host. The web
interface uses JavaScript and Python uses NumPy. Neither requires a decoder
compiler, native extension, WebAssembly asset or Git submodule. There is no
decoding-location or IQ-capture length setting.

Candidates include the preamble and PDU, bounded by 16384 samples. Failed
candidates remain available through packet subscriptions but do not create
packet clusters. No cross-antenna combining is performed by the decoder yet.

``BLEPacket.raw_capture`` identifies raw candidate records.
``BLEPacket.host_decoded`` reports successful host decoding. ``qualified`` and
``crc_valid`` describe the resulting packet. ``bytes(packet)`` preserves the
original sensor record. Gain, effective antenna feed, IF, timestamps and receiver
epochs remain capture metadata. Previously recorded BLQ5 files with sensor-decoded
metadata can still be read, but current firmware only produces raw candidates.

Decoder validation
~~~~~~~~~~~~~~~~~~

Both implementations are tested with deterministic GFSK waveforms covering
LE1M and LE2M, frequency offsets, receiver DC offsets and corrupted CRCs.
Controller tests can also compare NumPy results directly::

    python tests/test_ble_decoder.py --python-repo /path/to/pyespargos

The repositories build independently. The controller serves its JavaScript
decoder as an ordinary web asset and Python installs without compiling code.

LE Coded reception
~~~~~~~~~~~~~~~~~~

The ``LE1M`` receiver setting captures 1 Msymbol/s signals, including LE Coded.
The UI calls it ``LE 1M / Coded``. Host decoders recognize the coded preamble,
recover the coding indicator and decode S=2 or S=8 using soft-decision Viterbi
FEC before dewhitening and checking the advertising CRC. No sensor decoder or
additional sensor mode is needed. LE2M uses the separate ``LE2M`` setting.

``BLEPacket.phy`` identifies the decoded PHY (0=LE1M, 1=LE2M, 2=LE Coded).
``coding`` is 0 for uncoded packets, 2 for S=2 and 8 for S=8.
``capture_phy`` retains the receiver's modem setting. Calibration is bound to
that setting, so receiving a coded packet does not invalidate an LE1M reference
calibration. Waveform processing uses the coded packet duration.

Complete packet decoding still requires the preamble through the CRC and FEC
termination to fit in the 16384-sample capture buffer. This provides about 4 ms
at 4 MSa/s, 2 ms at 8 MSa/s or 1 ms at 16 MSa/s, less capture margins. Long coded
PDUs can exceed this limit and cannot yield a verified CRC. The receiver stays
on the selected channel and does not follow extended-advertising AuxPtr hops.
