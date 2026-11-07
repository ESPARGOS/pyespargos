# Wireless signal camera

Display directions estimated from raw IQ as short flashes over a live camera
image. This demo uses the current application framework and `Pool.iq`.
It does not decode packets, look for WiFi preambles, or require a particular
modulation. The default 2472 MHz tuning is an example; select the frequency,
receiver filter and sampling rate for the signals you want to observe.

From the repository root, with the demo dependencies installed:

```sh
python demos/camera/main.py -s 172.16.0.228 \
  -o 'app.camera.device="MX Brio"' \
  -o 'app.camera.format="1920x1080"'
```

Use `--no-camera` to display on black. RX opens the shared IQ receiver controls;
App opens detection, presentation and camera controls. Calibration runs on
startup and after incompatible receiver changes. Exit restores WiFi mode.

## Capture and display

All three trigger modes are supported. Signal is the default, with a 10 ms
holdoff and 56 chunks per event. It captures any signal crossing the amplitude
threshold, including unrelated devices. Automatic threshold adaptation and
alternation between the R and L feeds can each be disabled in App.
Signal captures currently require one ESPARGOS board: independent board
triggers do not form a synchronized combined-array event. The shared IQ
receiver retains failed acknowledgements and retries them from its existing
processing loop, including when no more captures arrive. Interval and
Accumulate support configured combined arrays.

For frequent short observations, start with Interval / 1024 interval chunks /
1 burst chunk. For Accumulate, try 4096 interval chunks / 4 vector chunks.
Accumulation favors signals repeating coherently at the accumulation period;
nonperiodic bursts can be attenuated. Mode and timing are editable in RX, or:

```sh
python demos/camera/main.py -s 172.16.0.228 \
  -o 'app.capture.trigger.mode="interval"' \
  -o app.capture.trigger.interval_chunks=1024
```

Each capture is split into time/frequency emissions above an adaptive noise
floor. Each emission gets an independent spatial covariance and direction
estimate. Continuous signals are retained using a spatial noise estimate,
provided that signal modes do not occupy the whole antenna space. Detection
has no minimum WiFi-like bandwidth. DC rejection excludes five center FFT
bins by default; disable it for signals there, with the possibility of ADC
leakage appearing as a source.

Burst flashes use a compact marker at each estimated peak, with brightness
based on received power weighted by direction quality. Weak background signals
therefore do not appear as bright as a nearby strong transmitter. MUSIC uses
MDL source-count estimation and paired 2-D root-MUSIC on the covariance.
Automatic source counts also exclude modes more than 12 dB below the strongest
noise-subtracted eigenvalue (`app.beamformer.source_dynamic_range_db`). This
limits spurious roots from small calibration/array-model errors at high SNR;
it can also omit a genuine weak simultaneous source. Explicit source counts
bypass that guard. Source powers use a nonnegative covariance fit, avoiding
unstable inverse-manifold powers for nearly coincident roots.
Bartlett uses a 2-D FFT of spatial covariance lags. Neither method scans a
dense steering grid. Coherent multipath can still collapse to one observable
mode and bias its estimated direction. **The flash
width is a presentation setting, not the array's angular resolution or an
uncertainty estimate.** Spatial spectrum
mode displays the Bartlett response or the rendered MUSIC roots for diagnosis. The optional
Azimuth flashes display uses vertical stripes and explicitly omits elevation;
it is useful when a short vertical aperture or reflections make elevation
unreliable. Two-dimensional Burst flashes remain the default. The default
120 ms exponential persistence fades a burst below the 12 dB display range
in about 330 ms. Freeze holds the radio overlay while live video continues; Clear removes its history. Directions outside the
camera field of view get explicitly labeled edge arrows when markers are
enabled; these are not projected onto objects inside the image.

Processing runs in a persistent spawned process, separate from acquisition and Qt. A bounded queue discards old captures when input
exceeds processing capacity. The status shows processed captures per second,
DSP time, queue-plus-processing latency, and skipped captures; that latency does
not include RF acquisition, transport or screen refresh. The overlay updates
at up to 40 Hz independently of video. Power is received digital power in
dBFS, not calibrated incident RF power.

## Camera alignment and interpretation

Camera formats include the pixel representation; resolution-only selectors
prefer JPEG when available to reduce USB traffic. Both camera demos disable
swap-interval waiting so an idle compositor cannot throttle camera delivery.

Camera device, format, horizontal/vertical field of view, flips and azimuth /
elevation shifts are shared with the WiFi CSI camera. Set FOV for the selected
camera mode, then use several known source positions to adjust mounting
shifts. Natural camera orientation is the default. Positive array azimuth
(front-view right) appears to the left in the forward-facing camera image.
A mounting translation also causes range-dependent parallax that fixed angle
shifts cannot fully remove.

Different feeds can reveal different paths. They do not guarantee rejection
of multipath; reflected arrivals can be strong and appear in the wrong place.
The current single-dominant-direction estimator cannot reliably separate
simultaneous emitters occupying the same time/frequency cells. A two-row array
also has limited elevation information. The projection assumes the front
hemisphere; a planar array has a front/back ambiguity. No MAC address filter is available
because this demo does not decode WiFi.

## Shared implementation

`demos/common/camera` owns video capture, camera settings, camera/beamspace
projection, marker projection, texture delivery, overlay shaders and common
Qt properties. Both this demo and `demos/wifi/camera` use those components.
`espargos.array_processing.steering_vectors_2d` supplies the steering grid;
IQ uses physical spacing and the selected frequency. IQ capture controls and
waterfall rendering come from `demos/iq/common`. Only IQ emission detection,
its presentation policy and acquisition management live here.

Shaders are precompiled in the shared directory. After changing GLSL, run
`demos/common/camera/compile_shader.sh` (or `.bat`) with Qt Shader Baker
available. Tests in `tests/test_camera_*.py` cover generic emissions, noise,
projection/model sharing and calibration filter restoration.

The waterfall retains every 256-sample FFT row from each processed capture,
including while the detector warms up. GUI refreshes do not pool rows. At
80 MS/s each row represents 3.2 µs of captured samples; gaps between captures
are omitted. The rolling history naturally discards its oldest rows.

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
