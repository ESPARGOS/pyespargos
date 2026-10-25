# MUSIC Spatial Spectrum

Plot arrival directions from WiFi CSI using a single ESPARGOS array or a
combined rectangular layout:

```sh
python demos/wifi/music-spectrum/main.py -s 192.168.0.234
python demos/wifi/music-spectrum/main.py -c config/aperture-kit-8x4.yml
```

Use a WiFi transmitter on the receive channel. For channel 5, HT20, add
`-o pool.channel=5 -o pool.secondary_channel=0`. Calibration runs at startup.
Combined boards need a shared reference and the cable lengths in the layout.
Use antennas with the same polarization.

The demo averages covariance across packets, array rows and subcarriers, then
scans along the array's horizontal axis. More columns give a larger aperture.
Rows provide additional observations rather than an elevation estimate.
The displayed angle equals azimuth for signals in the horizontal plane.
Elevated signals appear closer to the center.

By default, MUSIC assumes one dominant signal component. Change **Signal components** in the App drawer for multiple independent
components. The control allows fewer components than array columns.
Coherent reflections can violate this assumption and produce misleading peaks.
This demo does not apply spatial smoothing or estimate the source count.

Steering uses the 60 mm antenna spacing and the selected channel's center
frequency. This narrowband approximation can lose accuracy for large apertures
or signals far from broadside. The plot is normalized to its strongest peak
and is not a received-power measurement. Its dB range adjusts gradually over
a few seconds. Noise alone can also produce peaks.

For background, see [MUSIC direction finding](https://www.mathworks.com/help/phased/ug/music-super-resolution-doa-estimation.html).
