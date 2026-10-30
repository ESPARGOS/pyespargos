# IQ MIMO radar

This demo turns ESPARGOS One (single array or aperture kit) into a [MIMO Radar](https://www.radartutorial.eu/02.basics/en/rp18.en.html) with orthogonal OFDM waveforms.

A MIMO radar is a special kind of multi-static radar system that creates a larger *virtual* aperture from a smaller physical aperture using multiple transmitters.

In the case of this demo, we have two transmitters in the top left and bottom right corners of the physical array. In the case of a single ESPARGOS One, that looks like this:

|       | **Col 1** | **Col 2** | **Col 3** | **Col 4** |
|-----------|-------|-------|-------|-------|
| **Row 1** |  TX 1 |  RX   |  RX   |  RX   |
| **Row 2** |  RX   |  RX   |  RX   |  TX 2 |

The two transmitters both send OFDM waveforms, but with subcarrier interlacing (i.e., subcarrier 1 belongs to TX 1, subcarrier 2 to TX 2, subcarrier 3 to TX 1, and so on), so that the waveforms are orthogonal (in frequency).

The receivers observe changes in the OFDM subcarrier channel coefficients (due to reflections from moving objects) and visualize those. The MIMO radar principle provides a higher angular resolution that we could otherwise achieve with just the physical aperture.

To use this demo, enable transmit authorization in each controller's web interface, then run:

```sh
./demos/iq/mimo-radar/main.py -s 192.168.1.2
# Or use a combined array:
./demos/iq/mimo-radar/main.py -c config/aperture-kit-8x4.yml
```

The demo calibrates at startup and mutes transmission when closed.

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

Set **Max range (m)** in the right drawer to search farther away. Both plots
and the range processing grid update without recalibration. `0` keeps the
sample-rate-dependent default. The requested limit is capped below the pilot's
unambiguous range (about 240 m at 80 MSa/s or 959 m at 20 MSa/s); lowering the
sample rate can extend this limit. The drawer shows the effective range.
The grid has 121 points, so a wider view has coarser range sampling.

For example, to show simulated targets out to 900 m:

```sh
./demos/iq/mimo-radar/main.py -s 172.16.0.241 \
  -o app.sample_rate=20 -o app.range_maximum=900
```
