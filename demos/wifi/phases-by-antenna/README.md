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

The phase grid and radian labels are shared with the BLE phase demo. Random
polarization shows two stacked arrays, with feed 2 above feed 3. Each array
estimates phase independently using only observations on its feed. Other
RF-input settings show one array.

Set backlog size in the App drawer or with `-o backlog.size=32`. This demo does
not have a `-b` option.
