# IQ MIMO radar

Shows moving reflections in range–Doppler and range–azimuth plots. Two opposite
corner sensors transmit OFDM waveforms; the remaining sensors receive.

Enable transmit authorization in each controller's web interface, then run:

```sh
./demos/iq/mimo-radar/main.py -s 192.168.1.2
# Or use a combined array:
./demos/iq/mimo-radar/main.py -c config/aperture-kit-8x4.yml
```

The demo calibrates at startup and mutes transmission when closed. Radar controls
are in the left drawer, display settings in the right. The footer shows capture
coverage, plot rate, and estimated RX headroom with peak dBFS. Averaging can hide
brief clipping, so headroom is only an estimate.

All settings are available in the GUI and the YAML `app` section. Use `-o` to
override them, and `--help` to list keys and defaults:

```sh
./demos/iq/mimo-radar/main.py -s 192.168.1.2 \
  -o app.frequency=2412 -o app.integration_time=0.2
```

Changing frequency, sample rate, or calibration method recalibrates the array.
Integration time, TX amplitude, RX gain, and antenna feeds update without
recalibration. Shorter integration times follow faster motion; lower sample
rates extend the range at the cost of range resolution.

See [IQ Radar](../../../docs/iq-radar.rst) for background and API documentation,
and [mimo_processing.py](mimo_processing.py) for processing code. After editing
shaders, run `./demos/iq/mimo-radar/compile_shader.sh`.
