# BLE camera

Select Bluetooth LE advertisers and display their arrival directions over a
live camera image. The video capture, alignment controls and overlay shader
are shared with the WiFi and IQ camera demos.

From the repository root, with the demo dependencies installed:

```sh
python demos/ble/camera/main.py -s 192.168.0.234
```

Use `--no-camera` (or `-o app.camera.enable=false`) for an overlay on black. To select advertisers on startup:

```sh
python demos/ble/camera/main.py -s 192.168.0.234 -o app.camera.enable=false \
  -o 'app.receiver.advertisers=["c8:12:0b:62:de:49"]'
```

The compact overlay lists MAC addresses, packet counts and seconds since the
most recent packet. Check one or more advertisers to show their
spatial spectra. Unselected advertisers remain discoverable. Old observations
fade and disappear after one second. The list is bounded to 128 advertisers,
with selected rows retained when older unselected rows are replaced.

Open RX for receiver settings and App for the shared camera controls, including
Camera or Beamspace display. Both drawers stay closed until opened.

The receiver listens on one channel at a time. RX exposes all 40 channels,
LE 1M and LE 2M, automatic or fixed gain, sample rate and capture size.
Settings apply immediately. Channel, PHY and expert gain changes
invalidate calibration. Feed, ordinary gain, sample rate and capture length
changes preserve it. The highlighted Trigger Calibration
button in RX acquires a fresh reference when needed.
First polarization and automatic gain are the defaults. The manual gain slider
also sets the fixed gain used briefly for reference calibration.

RF input offers Iso, both antenna polarizations, Random and Ref. Random chooses
an antenna input independently for every sensor capture. The firmware reports
the effective input, and the camera combines the two polarizations' spatial
powers separately to avoid mixing their unknown signal phases.

Beamspace mode shows the shared azimuth and elevation coordinate diagram,
with angle labels and the visible hemisphere boundary.

## Reference calibration

Calibration runs on startup. In the array's web interface, set BLE Reference
Generation to During Calibration or Always, with the internal reference source.
The demo briefly selects REF, averages eight complete observations from the reference advertiser,
then returns to the antenna input. Calibration also corrects the PCB reference-path delays. It does not change persistent reference
settings. If calibration fails, advertiser discovery continues and the
Trigger Calibration button in RX retries it. No direction overlay is displayed without a
successful calibration.

The demo requires API 5 controller firmware and BLQ5 sensor captures. It
currently supports one eight-sensor array. Direction estimation supports LE 1M
and LE 2M with either automatic or fixed gain. LE Coded is unsupported.
LE 2M calibration needs
an external reference because the controller generates LE 1M only. Set its
advertiser address with `-o 'app.receiver.reference_address="aa:bb:cc:dd:ee:ff"'`.
Other advertisers are excluded from calibration even if their packets leak into REF. Each spatial
observation requires all eight antennas. At least one sensor must report a
valid packet CRC. Other sensors may contribute only after a unique, coherent
waveform match. Scan requests and connection requests are excluded because
their transmitters are not the advertiser named in the request.

App settings offer Bartlett, MUSIC and Root-MUSIC. MUSIC uses a spatial
spectrum, while Root-MUSIC shows estimated direction markers. With Multipath enabled, both use forward/backward spatial smoothing,
and Root-MUSIC estimates multiple significant paths per advertiser. Smoothing
uses overlapping 2×3 subarrays, trading angular resolution for robustness to
coherent reflections. A reported path is not necessarily the direct path. The BLE backlog defaults to at most 16 observations
and 250 ms, whichever limit is reached first. Packets from different advertisers
never share a covariance. Measured amplitudes are retained for multipath
processing instead of reducing every sensor to unit magnitude. A single complete
fixed-feed observation can already provide a spatial covariance, which keeps
slow advertisers usable. Without polarization reconstruction, Random contributes
only same-feed pairs. MUSIC waits until the backlog covers all sensor pairs.

Live processing keeps only the newest pending observation per selected advertiser.
Phase fitting does not block packet assembly or the UI, and unselected advertisers
are discovered without computing their directions. All three beamformers use the selected packet history and the full usable
packet IQ covariance. Independent packet phases are combined incoherently.

Feed and gain changes retain reference calibration. Measured per-packet gain
corrects phase and amplitude under AGC. Firmware must support BLQ5 gain metadata.
An observation with a changing/unknown gain is skipped without dropping the
advertiser or invalidating calibration.
Channel, PHY and expert gain changes require calibration.

Directions use narrowband spatial processing. Multipath can produce
strong reflected directions, and the short vertical aperture limits elevation
resolution. A planar array has front/back ambiguity. Calibrate the camera's
field of view and mounting offsets in App using known transmitter positions.
No CTE is required. The demo does not follow connections or channel hopping.

On normal exit, the demo restores the previous controller role, per-sensor
modes and BLE receiver settings, and releases its stream. An interrupted process
cannot restore hardware settings.

The demo uses the public `board.ble` and `pool.ble` APIs. Packet discovery, raw
captures, event association, phase estimation and reference calibration are
available independently of Qt and this camera. See `docs/ble-reception.rst`
and `docs/measurement-architecture.rst` for other consumers and extension points.

## Polarization and multiple targets

Select any number of advertisers in the overlay. Each keeps an independent
short backlog, spatial result and MAC-labeled markers. Multiple markers with
the same MAC are candidate paths from one advertiser, not extra transmitters.

App → Polarization offers Ignore, Incorporate and Show. Incorporate uses both
feeds and the shared frequency-dependent antenna Jones model before beamforming.
Show additionally displays the same animated H/V polarization ellipses as WiFi.
Selecting either enables Random RF input. Automatic gain is supported.

BLE packet RMS amplitudes are normalized for measured gain and retained
alongside calibrated phases. Mixed-feed
observations connect both feed domains across packets, removing each packet's
unknown common complex scale. Results require connected coverage of all feeds
within the bounded backlog. While coverage is incomplete, ordinary same-feed
beamforming remains available. A recent polarization estimate survives a short
coverage gap, but expires after the configured backlog age. Missing or fully
unpolarized estimates do not draw stationary dots. Alternating
the entire array between fixed feeds
cannot establish their relative phase and is deliberately rejected. Increase
the backlog age only if a slow advertiser does not provide enough observations.

Polarization is an estimate under a locally stable channel and the supplied
antenna model, not a separate amplitude-calibrated polarization measurement.
Independent advertisers are combined as coherency matrices for visualization,
never by adding their complex fields. Overlapping mixed polarizations reduce
the displayed polarization degree.

MUSIC and Root-MUSIC allow different arbitrary complex H/V polarizations for
resolved paths. Their path polarizations are fitted jointly after estimating
directions, so a dot does not inherit another path's Bartlett sidelobes.
The display keeps each ellipse's arbitrary common phase continuous across
frames. This avoids animation jumps when H and V have nearly equal magnitude.
The finite array aperture and antenna response model still limit accuracy.

In Random mode, firmware chooses an antenna feed after each qualified packet,
after storing that packet's metadata. Noise candidates retain the current feed.
The wire record carries the
actual feed used for that capture (2 or 3), separately from the requested Random
mode (4). A random choice can repeat the previous feed. This is not forced
alternation of the whole array.

Fresh spectra use automatic exposure like the WiFi camera. Age opacity is
applied after display contrast, with a 200 ms grace period for normal packet
and UI scheduling gaps. A newer measurement replaces the previous direction
immediately. Once a source has a polarization-corrected estimate, temporary
feed-coverage gaps do not substitute an uncorrected spectrum with a different
intensity and spatial response. The last valid result instead ages out normally.

When polarization is set to Show, Grid spacing and Phasor speed control the dots.
Zero speed pauses the animation. These controls share their implementation with
the WiFi camera and do not reset the measurement backlog.

Both MUSIC methods restrict automatic source selection to covariance modes
within 10 dB of the strongest signal mode. This prevents weak calibration and
random-feed reconstruction errors from consuming the noise subspace. It also
limits the ability to distinguish much weaker reflected paths. Random-feed
polarization still needs sufficient recent coverage of both feeds on every
sensor. An undersized or very short backlog can produce gaps in that estimate.
