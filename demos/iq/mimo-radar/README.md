# IQ MIMO radar

Shows moving reflections in range–Doppler and range–azimuth plots. Two opposite
corner sensors transmit OFDM waveforms, and the remaining sensors receive.

Enable transmit authorization in each controller's web interface settings, then run:

```sh
./demos/iq/mimo-radar/main.py -s 192.168.1.2
# Or a combined array:
./demos/iq/mimo-radar/main.py -c config/aperture-kit-8x4.yml
```

The demo calibrates at startup and mutes transmission when closed.

- **Left drawer:** start/stop, RX sample rate, antenna feeds and calibration.
- **Right drawer:** color scale, color limits and velocity view.
- **Footer:** capture coverage and plot rate.

Lower sample rates extend the displayed range but reduce range resolution.
Changing the rate recalibrates the array. Use `--help` for command-line options.

See [IQ Radar](../../../docs/iq-radar.rst) for background and API documentation.
Processing is in [mimo_processing.py](mimo_processing.py). After editing the
shaders, run `./demos/iq/mimo-radar/compile_shader.sh`.
