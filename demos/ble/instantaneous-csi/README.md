# Instantaneous BLE CSI

Show power (dB) and phase (rad) in the same two-plot layout as the WiFi
instantaneous-CSI demo, over the smaller BLE bandwidth. A received antenna waveform provides the reference, so transmitted
pilot symbols are not needed.

```sh
python demos/ble/instantaneous-csi/main.py 192.168.0.234 \
  -o app.advertiser=30:ed:a0:e5:e5:72
```

Select one advertiser in the shared BLE overlay at the top right. It shows
MAC addresses, packet counts and seconds since the last packet. RX reuses the BLE receiver drawer and reference
calibration controls. App allows comparison with uncalibrated responses and
capture-timing correction disabled. The latter restores the estimated trigger
shift's frequency slope and IF phase, which is useful for debugging.

LE 1M shows ±0.5 MHz and LE 2M shows ±1 MHz. Spectral bins with little signal
energy or low coherence are excluded. The channel response is relative to the received reference. Waveform
alignment removes packet timing before the calibrated curves are computed.

The demo uses the same full-packet alignment and shared phase/time calibration
model as the BLE camera. Receiver settings are restored when the demo closes.
Use `-o app.nfft=256` for a denser frequency grid, at the cost of fewer spectral
averages per packet. LE Coded direction/channel estimation is not supported.
