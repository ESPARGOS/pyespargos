# Instantaneous BLE CSI

Show power (dB) and phase (rad) in the same two-plot layout as the WiFi
instantaneous-CSI demo, over the smaller BLE bandwidth. A received antenna waveform provides the reference, so transmitted
pilot symbols are not needed.

```sh
python demos/ble/instantaneous-csi/main.py 192.168.0.234 \
  -o app.advertiser=30:ed:a0:e5:e5:72
```

For multiple boards, pass their hosts as one comma-separated argument:

```sh
./demos/ble/instantaneous-csi/main.py topleft.local,topright.local,bottomleft.local,bottomright.local
```

Each board contributes eight curves in host-list order. Each board uses its own packet phase reference.

Select one advertiser in the shared BLE overlay at the top right. It shows
MAC addresses, packet counts and seconds since the last packet. RX reuses the BLE receiver drawer and reference
calibration controls. App allows comparison with uncalibrated responses and
capture-timing correction disabled. The latter restores the estimated trigger
shift's frequency slope and IF phase, which is useful for debugging.

LE 1M shows ±0.5 MHz and LE 2M shows ±1 MHz. Spectral bins with little signal
energy or low coherence are excluded. The channel response is relative to the received reference. Waveform
alignment removes packet timing before the calibrated curves are computed.
Each board keeps one antenna as its 0 dB and 0 rad reference across packets.
If it disappears, the plot holds the last measurement for up to 250 ms before
choosing another antenna with overlapping valid bins. In **App → Packet backlog**, **Packets** and **Max age** select the averaging
window (default 8 usable advertisements, 500 ms). Set Packets to 1 for a
single-packet display. **Min. antennas** sets how many antennas must each
have at least two usable frequency bins in common with the reference for a
packet to enter the backlog. The count includes the reference and defaults to
2 of 8. For example, use `-o app.min_antennas=6` to require six antennas. Each frequency bin averages linear power and circular
phase over observations with matching reference antenna and feed routes.
The CLI equivalents are `-o app.backlog.size=8 -o app.backlog.max_age=0.5`. The vertical scale leaves headroom so small power fluctuations
do not repeatedly resize the plot. It clears when the last usable measurement exceeds Max age.

The demo uses the same full-packet alignment and shared phase/time calibration
model as the BLE camera. Receiver settings are restored when the demo closes.
Use `-o app.nfft=256` for a denser frequency grid, at the cost of fewer spectral
averages per packet. Reference calibration requires LE 1M or LE 2M.
