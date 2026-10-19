# IQ transmit beams

A Qt Quick demo for one array or a combined aperture, using the same dark
Material theme and two drawers as the receive demos. Requires the current
controller/sensor firmware, pyespargos dependencies, `PyQt6` and `PyYAML`.

From the pyespargos directory:

```sh
./demos/iq-transmit-beams/iq-transmit-beams.py --config config/aperture-kit-8x4.yml
```

For a single array:

```sh
./demos/iq-transmit-beams/iq-transmit-beams.py 192.168.1.2
```

A comma-separated host argument overrides the hosts in `--config`, in board
order. Multiple arrays require a layout: simply concatenating antenna IDs
would produce incorrect steering. Layout cells are 60 mm apart by default
(`--spacing` overrides this in metres). Combined arrays need a shared clock
and forward REF distribution for calibration.
The demo uses WebSocket streaming so a lost UDP packet cannot discard a
reference capture during calibration.

Open **⚙ TX** for RF settings, calibration and Start/Stop. **App ⚙** contains
display and reference-monitor settings. Waveforms and steering stay in the
main view.

On startup, all sensors enter IQ TX mode and calibrate automatically through
Reference. A calibration animation remains visible until this finishes, then
transmission starts on Antenna R. Transmit authorization must already be enabled
on each board. If calibration fails, transmission stays muted.

Choose the waveform and drag the beamspace handles to steer. The TX drawer lets
you select Reference, Antenna R, Antenna L or Isolation. After changing frequency
or replay length, click **Calibrate RX + TX**, then **Start transmission**.
Stop replaces the samples with silence. Closing the app mutes all boards.

Each of up to four beam tabs selects its waveform controls. All beams appear
on one beamspace plot as numbered, colored handles. Drag any handle to steer
that beam independently. Select its tab or the button below the plot when
handles overlap.
Waveforms include CW, two-tone, LFM, noise, QPSK and uploaded I/Q. LFM defaults
to a 32-sample sweep from −40 to +40 MHz, with zero delay. All beams share the
80 MSa/s sample rate and replay length. Beam weights are divided by the number
of beams, matching the web interface. Beamspace steering uses the entire aperture,
including board rotations from the layout, with optional beam-squint correction.

The power lock limits amplitude to 0.4, gain A to 2 and gain B to 0. Higher
gains can reset a complete aperture kit under simultaneous TX load. Unlocking
exposes the extended range. These are demo limits, not API restrictions.

Settings apply automatically. The plot retains the mouse grab during fast drags, including movements outside
its boundary. Only the latest pending steering position is
sent. Waveforms are synthesized and cached on the sensors, and file data is
uploaded once per beam/source change. Changing replay length mutes TX and
restarts replay; recalibrate afterward. Frequency changes also require calibration. Gain and steering changes preserve replay timing. Gain changes only update the transmitter, without resending waveforms.

I/Q files can be complex NumPy `.npy` arrays or two-column `I,Q` CSV/text files.
They must contain exactly the selected number of samples. Each beam can use a
different file. Samples are normalized before applying peak amplitude.
The source preview is illustrative; use the reference monitor to inspect the
actual signal. The framed waveform preview supports cursor-centered wheel zoom and
horizontal drag panning. Shift-scroll zooms amplitude, and double-click resets the view.

The reference monitor starts automatically and stays enabled while the demo
is open. Its settings apply immediately from the right drawer. It automatically uses the combined array’s detected master
controller and shares its existing stream
with sensor acknowledgements. Its waterfall, spectrum and I/Q trace use the
controller's received samples. The waterfall uses the signal analyzer’s
shared viridis renderer and fixed −90 to 0 dBFS scale. On an amplified aperture kit, the controller
monitor cannot receive signals backwards through the REF amplifier.

Transmit authorization remains in each controller's Settings. The app does
not change it. Recommended amplitude and RF gain ranges are enforced in the
UI. Separate gain A and B sliders and the white power-limit lock are in the
left drawer. Unlocking turns the lock red and enables extended ranges. IQ TX is experimental and can
produce high EIRP.

## Reusing TX features

The launcher only selects the shared application and QML composition. Other
demos can use the package API and the Qt components in `demos/common`:

- `espargos.iq_tx.TXLayout` and `TXSession`: Qt-independent aperture geometry,
  synthesis recipes, upload caching, calibration, monitoring and TX lifecycle.
  `TXSession(layout, pool=running_iq_pool)` shares an existing pool and streams.
  Closing this attached session mutes TX without closing the borrowed pool.
- `iq_tx_controller.IQTxController`: asynchronous Qt adapter with one hardware
  operation in flight and a coalesced queue of edits. It accepts a
  `session_factory` for an existing pool or testing. Create it on the Qt thread,
  call `connectArray()`, and wait for `closed` after calling `close()`.
- `IQTxDrawer`, `IQTxWorkspace`, `IQTxBeamEditor`, `IQTxBeamspace` and
  `IQTxMonitorSettings`: composable QML controls taking a `controller` property.
- `iq_tx_plot.IQTxPlot`: source preview and controller monitor with pan/zoom.
  Register it as `IQTxPlot` in `ESPARGOS.Tx` 1.0 when embedding these components.
- `iq_tx_application.ESPARGOSTxApplication`: ready-made shared application
  setup, configuration loading, QML registration and shutdown handling.

Hardware operations and file loading run off the UI thread. The UI owns
editable state, so delayed replies cannot overwrite newer settings. The queue
keeps the latest steering position, gives Stop priority over pending updates,
and attempts to mute all boards if an update fails. Normal close waits for
mute acknowledgements. Shutdown failures are shown explicitly.

If communication fails and stopping TX cannot be confirmed, use **Reconnect**
after the boards return. This mutes them, reconnects the streams and reads their
actual state. Recalibrate after a reset before starting transmission again.
