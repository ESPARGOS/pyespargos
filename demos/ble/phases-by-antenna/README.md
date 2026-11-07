# BLE phases by antenna

Run from the pyespargos directory:

```sh
python demos/ble/phases-by-antenna/main.py -s 192.168.0.234
```

Use `-c layout.yml` for a configured antenna layout, including board rotation.
BLE phase measurements currently support one board. Bare positional hosts are
not accepted.

Select an advertiser in the top-right overlay. The grid follows the physical
antenna layout and shows phase in radians relative to the first valid antenna.
Each value uses the usable captured packet waveform, with the same timing, IF
and reference correction as the BLE camera and instantaneous CSI demos.
Missing or stale measurements turn gray after one second.

The RX drawer contains receiver settings and reference calibration. The App
drawer can disable calibration for diagnostics. Standard `-o` configuration
works here, including `-o app.advertiser=30:ed:a0:e5:e5:72`.

Random polarization shows two stacked arrays, with feed 2 above feed 3.
Each array has its own phase reference and uses only measurements from that
feed. Recent packets supply the missing sensor pairs as the random switches
change. Other RF-input settings show one array. Tiles display radians only.

The antenna grid is shared with the WiFi phase demo. Discovery, selection,
receiver control and background processing are shared with BLE instantaneous CSI.
