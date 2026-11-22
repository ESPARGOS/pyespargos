WiFi IQ Reception
=================

The WiFi modem can report a channel estimate for a packet it decodes.
Sometimes the measurement you need is missing from that report. WiFi IQ
records the received waveform at each antenna at 20 MSa/s, so you can work
with the training signal itself. Three cases are particularly useful:

* **802.11b.** These packets use a spread-spectrum preamble and have no OFDM
  long training field. To measure the channel, compare the received preamble
  with its known chip sequence. The SDK stores this estimate in the ``dsss`` backlog field.
* **Multiple transmit streams.** The legacy preamble gives a common channel
  estimate even on HT or HE packets carrying multiple spatial streams. To
  distinguish the transmit streams, you need the stream-specific training
  fields later in the packet. The SDK estimates each transmit-to-receive
  channel from those fields on HT20 and HE SU20 packets.
* **Weak packets.** A packet may reach one antenna clearly and be too weak
  to trigger another. Shared triggering is on by default. A detection on one
  sensor can trigger captures on its peers. Those samples are available for
  joint analysis even when a peer cannot identify the transmitter. In the
  CSI backlog, an antenna contributes only when the receiver can associate
  its capture with the frame and validate the training.

For modem-reported CSI, see :doc:`wifi-reception`.

Capture packets
---------------

Subscribe on a board to receive each antenna's report, including captures
without a decoded transmitter address. The callback runs on the board's
dispatch thread. Copy or queue the reports you want to process later::

   from queue import Empty, Queue
   from espargos import Pool

   with Pool("192.168.1.2") as pool:
       pool.configure(mode="wifi_iq", channel=6)
       board = pool.boards[0]
       reports = Queue()
       board.wifi_iq.subscribe_packets(reports.put)
       try:
           report = reports.get(timeout=5)
       except Empty:
           print("No packets on channel 6")
       else:
           packet = report.payload
           print(report.antenna_id, packet.source_mac, packet.samples.shape)

``samples`` contains complex ADC counts. A metadata-only report has no
samples. To use :doc:`packet-decoders` directly, pass ``packet.raw``, which
preserves the ADC words in the decoder's input format. A shared trigger
can produce separate waveform and modem reports for the same packet.
``board.wifi_iq.get_config()`` returns the capture settings. Change the
channel, gain and trigger for all participating WiFi IQ sensors on that
board together.

Collect channel estimates
-------------------------

The native WiFi and WiFi IQ receivers use the same ``CSIBacklog`` and MAC
filters. Use ``pool.wifi_iq`` for CSI estimated from packet IQ::

   from espargos import CSIBacklog, MACFilter, Pool

   with Pool("192.168.1.2") as pool:
       pool.configure(mode="wifi_iq", channel=6)
       log = CSIBacklog(pool.wifi_iq, fields=["lltf", "dsss", "mac", "host_timestamp"],
                        apply_calibration=False, size=32)
       # Optional: select one transmitter using its 12 hexadecimal digits.
       # log.add_filter(MACFilter("3cdc75888801$"))
       pool.start_processing()
       input("Press Enter to inspect the captures\n")
       ofdm, dsss, addresses, times = log.get_multiple(["lltf", "dsss", "mac", "host_timestamp"])

The ``lltf`` estimate uses the two repeated legacy long training symbols (L-LTF)
of an OFDM packet. The 53 entries cover subcarriers -26 through +26,
with the untransmitted DC bin interpolated. HT and HE packets also carry
this legacy field. It measures their common preamble, not the individual
spatial streams. For 802.11b, the receiver fits the known Barker pulse to the
captured SYNC symbols. The ``dsss`` estimate has 35 frequency bins spaced
312.5 kHz apart, from -5.3125 to +5.3125 MHz relative to the channel center.
Bins outside the training signal’s main lobe are omitted. For each packet,
the field belonging to the other PHY contains NaNs.

For OFDM, set ``pool.wifi_iq.estimator`` to choose the waveform fields. The SDK default,
``"training"``, uses the L-LTF alone. With ``"headers"``, a sensor can also
use L-SIG and HT-SIG when their codewords pass validation. With ``"payload"``,
at least three antennas can use each other's symbol decisions to refine a
legacy OFDM or single-stream HT20 estimate. If the capture ends early or
the extra symbols are unreliable, the earlier fields still provide CSI.

Each sensor locates the training field independently. Moving the FFT window
by one sample adds a phase slope equivalent to 50 ns of delay. The SDK uses
the ADC ring positions reported by the sensors to put their FFT windows on
a common timebase. This preserves differences in propagation delay between
antennas. It requires sensor firmware that reports the ring position.
The packet timestamps alone have microsecond resolution.

The pool groups reports on each board by transmitter, frame identity and
training time. A spatially multiplexed frame can have a valid PHY header
even when the MAC address is unknown. With the shared trigger, the sensors
have a common timing edge. With independent triggers, the SDK uses previously
CRC-verified WiFi packets, including 802.11b, to learn the offset between
each pair of sample counters. Until then, it keeps observations with unknown
MAC addresses separate.
It can then distinguish two successive transmissions with identical PHY headers.
A MAC filter excludes observations with an unknown address.

The default callback waits for all sensors assigned to WiFi IQ on that board.
Pass a callback predicate to accept partial measurements.
Missing channels remain NaN in the pool's usual board, row, column layout.

Measuring multiple transmit streams
-----------------------------------

One receive antenna can measure two transmit streams because the transmitter
sends a different combination of those streams in each training symbol. With
eight receiving antennas and a two-stream packet, each measured frequency
can therefore have an eight-by-two channel matrix. The columns describe the
transmitter's spatial mapping, including any cyclic shifts it applied.

For one antenna report, use ``packet.spatial_csi``. For an array measurement,
subscribe to the WiFi IQ receiver::

   from espargos import Pool

   def channels(cluster):
       measurement = cluster.spatial_csi
       if measurement is None or measurement.spatial_streams < 2:
           return
       # board, antenna row, antenna column, space-time stream, frequency
       print(measurement.phy, measurement.mcs, measurement.coefficients.shape)
       print(measurement.frequencies_hz)

   with Pool("192.168.1.2") as pool:
       pool.configure(mode="wifi_iq", channel=11)
       receiver = pool.wifi_iq
       receiver.configure({"ofdm_words": 2048, "boot_sync": True})
       receiver.add_callback(channels, lambda c: c.is_complete or c.age >= 0.1)
       pool.start_processing()
       input("Press Enter to stop receiving\n")

The frequency offsets are relative to the channel center. HT20 provides
56 measured tones. For example, an 802.11n MCS 15 downlink gives a
coefficient array of shape ``(1, 2, 4, 2, 56)`` when all eight antennas on
one board receive it. The shared trigger gives the antennas the best chance
of capturing the same packet. For HE SU20, the spacing depends on the training format.
Pilots shared between streams are omitted. Missing antennas have NaN
coefficients. A capture that ends before all training symbols arrive has no
spatial estimate. Use the ``spatial_csi`` backlog field to retain successive
measurements. With calibration enabled, only frequencies covered by the
reference measurement have calibrated coefficients.

Phase calibration
-----------------

For direction finding, calibrate at the frequency you want to measure. With the board's internal WiFi reference configured
for generation during calibration, run::

   pool.configure(mode="wifi_iq", channel=6, rx_gain=30,
                  output="antenna_r")
   pool.wifi_iq.calibrate(board_index=0)
   log = CSIBacklog(pool.wifi_iq, fields=["lltf", "mac"])
   pool.start_processing()

Calibration temporarily routes the sensors to the reference input, measures
their relative phase at a fixed reference gain and restores the antenna settings.
During reception, the SDK uses the gain recorded during each packet's training
field to correct for gain-dependent phase shifts. An antenna whose gain changed
during training is left out of the calibrated estimate. Use ``pool.wifi_iq.configure(settings)`` to change settings during reception.
Recalibrate after changing channel or analog filter. Each board has its own phase reference.

Demos
-----

Run either demo from the SDK checkout. Both use the same **RX** drawer. Choose the channel, antenna feed,
gain mode and analog filter there. Enable **Automatic** to let each sensor set its gain. Turn it off to set
a common **RX Gain**. Under **Packet capture**, select the
capture duration, packet rate limit and which sensors can trigger a shared capture.
Edits take effect immediately. **Waveform fields** selects the estimator for both
demos and defaults to training plus header. A calibrated demo measures a new
reference when you change channel or analog filter. **Trigger Calibration** measures a new reference for those settings. The MAC
filter selects a transmitter from the received packets and can be changed
without recalibrating.

Camera demo
~~~~~~~~~~~

Launch the camera on the channel you want to observe::

   python demos/wifi_iq/camera/main.py -s 192.168.1.2 -o wifi_iq.channel=6 -o wifi_iq.rx_gain=30

The camera calibrates one array, then overlays the directions of received
802.11b and OFDM packets on the video. It uses the same beamformers and camera
alignment controls as the native CSI camera. In either WiFi IQ demo, the
**Training channel** selector switches between the common preamble and an
individual space-time stream. To start on stream 2, add
``-o csi.space_time_stream=2``. Add ``--no-camera`` to view the
radio overlay alone. Power in WiFi IQ mode is relative to ADC full scale.

Instantaneous CSI demo
~~~~~~~~~~~~~~~~~~~~~~

To inspect the channel on each antenna, run::

   python demos/wifi_iq/instantaneous-csi/main.py 192.168.1.2 -o wifi_iq.channel=6

The frequency view uses the latest packet to select Barker or L-LTF and plots
the magnitude and phase from backlog entries of that type. By default a packet
can appear even if only one antenna has usable training. Increase **Min. Antennas**
when you need the same packet on several antennas.
Use the settings drawer to switch to the impulse response, MUSIC or MVDR delay
profile. **Channel constellation** plots the complex channel coefficients on
fixed I and Q axes from -2048 to 2047, so changes in gain remain visible.

**Data symbol constellation** shows one received packet at a time. For 802.11b it shows
differential preamble symbols and, when captured, payload symbols or CCK chips.
For OFDM, the colors distinguish preamble, signaling, pilots and payload. The points are
equalized measurements, so their spread shows up in the plot. Payload points
require a complete data symbol and its training field. For a spatially
multiplexed packet, at least as many antennas as transmitted streams must
capture the symbol, and their channels must be sufficiently independent.
The receiver can separate up to four HT20 streams or eight HE SU20 streams.
A single antenna can still measure each stream's training channel. If the
capture ends during training, try a longer window in the RX drawer.
