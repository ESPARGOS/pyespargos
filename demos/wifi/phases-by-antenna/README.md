# Phases by Antenna

Show the received WiFi phase at each antenna, relative to the first antenna.
The same view supports one ESPARGOS array or a combined array layout.

For one array:

```sh
python demos/wifi/phases-by-antenna/main.py -s 192.168.0.234
```

For a combined array:

```sh
python demos/wifi/phases-by-antenna/main.py -c config/aperture-kit-8x4.yml
```

Use a WiFi transmitter on the receive channel. Set the channel with, for example,
`-o pool.channel=5 -o pool.secondary_channel=0` for channel 5, HT20.
Calibration runs at startup. Use `--no-calib` to display uncalibrated phases.
