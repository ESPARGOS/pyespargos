# Wireless signal camera

The IQ camera estimates arrival directions from synchronized raw IQ. It can
display WiFi, Bluetooth, tones and other signals without decoding packets.
Tune the receiver to the transmitter you want to see. For example, to look
at WiFi channel 10 on a single ESPARGOS board, run:

```sh
./demos/iq/camera/main.py -s 172.16.0.212 -o app.capture.rf_freq_hz=2457000000
```

Use `-c config/special-8x4-horizontal-kit.yml` for the combined array.
Use `--no-camera` to show the overlay on black. Without a frequency override,
the demo starts at 2472 MHz.

**RX** controls the frequency, gain, input and trigger. Startup synchronizes
and calibrates the array. After a change that invalidates calibration, open
RX and press **Sync + Calibration**. Changes to manual gain preserve calibration.
Expand **Show advanced settings** for the sample rate, capture path and filter.
If a controller reboots, the camera restores its capture settings. If another
client switches a board out of IQ mode, **Sync + Calibration** restores the
last requested settings across the array. Press it to resume direction
estimates in either case. Exiting the demo returns the sensors to WiFi mode.

**App** controls the detection threshold above the noise floor, beamforming
method, direction labels, optional waterfall and the shared camera settings.
The detection threshold selects emissions for display. The separate trigger
threshold in RX controls when the hardware records a signal capture.

## Choosing a trigger

* **Signal** is the default on one board. All eight sensors record the same
  56-chunk capture when a selected sensor crosses the amplitude threshold.
  The camera can already show a direction from synchronized chunks while
  the complete capture is arriving. The receiver adapts the trigger threshold
  to the observed noise. Uncheck **Automatic trigger threshold** in RX to
  set it manually.
* **Interval** is the default on a combined array. It samples all boards on a
  shared grid. Single-board signal triggers cannot form a synchronized event
  across independent controllers. The default interval of 4096 chunks at
  80 MS/s gives one snapshot every 13.1 ms. A shorter interval samples more often and uses
  more bandwidth. Short packets can fall between snapshots.
* **Accumulate** combines observations coherently at the configured period.
  This is useful for periodic signals.

Use `-o app.capture.trigger.mode=interval` to start a single board in interval
mode. One antenna feed stays selected until you change it in RX. Switching
feeds can change which direct or reflected arrival is strongest. Switching
between Signal and Interval at the same receiver settings preserves calibration.

## Reading the overlay

Each capture is split into emissions using an adaptive frequency-dependent
noise floor. The camera estimates a direction for each emission using MUSIC
or Bartlett, then combines recent spatial estimates. A new arrival updates
the overlay immediately after processing. With the default persistence, the
last detection stays bright for 0.5 s and fades over the next 0.2 s. This
keeps short packet gaps from looking like transmitter movement. Video updates
independently. During overload the camera processes the latest waiting capture.
Marker width controls the size of the drawing.

The optional waterfall shows the FFT rows from processed captures. At
80 MS/s each row represents 3.2 µs. Time between captures is omitted.
Power is digital power at the ADC in dBFS.

For narrowband measurements, select a frequency region with
`app.beamformer.band_limit=true`, `app.beamformer.band_low_mhz` and
`app.beamformer.band_high_mhz`. The default DC rejection excludes the five
center FFT bins. A tone at the center frequency needs
`app.beamformer.reject_dc=false`, or a small offset in tuning.
`app.beamformer.persistence_s` controls how quickly new direction estimates
replace earlier ones. `app.visualization.display_mode=spectrum` shows the
full Bartlett response or the MUSIC roots.

## Camera alignment and interpretation

Camera formats include the pixel representation. Resolution-only selectors
prefer JPEG when available to reduce USB traffic. All camera demos disable
swap-interval waiting so an idle compositor cannot throttle camera delivery.

Camera device, format, horizontal/vertical field of view, flips and azimuth /
elevation shifts are shared with the WiFi CSI camera. Set FOV for the selected
camera mode, then use several known source positions to adjust mounting
shifts. Natural camera orientation is the default. Positive array azimuth
(front-view right) appears to the left in the forward-facing camera image.
A mounting translation also causes range-dependent parallax that fixed angle
shifts cannot fully remove.

Different feeds can reveal different paths. They do not guarantee rejection
of multipath. Reflected arrivals can be strong and appear in the wrong place.
Simultaneous emitters occupying the same time/frequency cells need a resolvable
spatial covariance. Correlated sources can remain inseparable. A two-row array
also has limited elevation information. The projection assumes the front
hemisphere. A planar array has a front/back ambiguity. No MAC address filter is available
because this demo does not decode WiFi.

## Shared implementation

`demos/common/camera` owns video capture, camera settings, camera/beamspace
projection, marker projection, texture delivery, overlay shaders and common
Qt properties. This demo, `demos/wifi/camera` and `demos/ble/camera` use those components.
MUSIC estimates directions from polynomial roots and Bartlett uses a spatial
FFT. Both use physical antenna spacing and the selected frequency. IQ capture controls and
waterfall rendering come from `demos/iq/common`. Only IQ emission detection,
its presentation policy and acquisition management live here.

Shaders are precompiled in the shared directory. After changing GLSL, run
`demos/common/camera/compile_shader.sh` (or `.bat`) with Qt Shader Baker
available. Tests in `tests/test_camera_*.py` cover generic emissions, noise,
projection/model sharing and calibration filter restoration.

SciPy is optional (`pip install scipy`) and accelerates the short FFTs on
installations where NumPy's single-precision FFT is slower. NumPy remains
the fallback. Array algorithms are shared in `espargos.array_processing`.
Run the waveform-independent synthetic accuracy/timing experiment with:

```sh
OPENBLAS_NUM_THREADS=1 PYTHONPATH=. python tools/benchmark_array_processing.py --trials 100
```

The experiment covers independent sources at 0/10/20 dB SNR and reports
coherent reflections separately. These ideal calibrated-array results do not
establish accuracy in a room with multipath or antenna-model errors.
