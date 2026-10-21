# IQ transmit beamforming

Steer up to four independent waveforms on one array or a combined aperture.
Run from the pyespargos directory:

```sh
./demos/iq-transmit-beamforming/iq-transmit-beamforming.py -s 192.168.1.2
./demos/iq-transmit-beamforming/iq-transmit-beamforming.py -c config/aperture-kit-8x4.yml
```

Enable transmit authorization on each controller first. The demo puts all
sensors into IQ TX mode, calibrates through REF, then starts transmission on
the right antenna feed. Single arrays use monitor calibration; combined arrays
use reciprocity. Choose either method in the TX drawer for recalibration.
Reciprocity supports the reference network or either antenna feed; controller
monitor calibration needs a bidirectional REF path. Use `--tx-calibration` and
`--calibration-path` to choose the startup method and path.

Choose waveforms in the beam tabs and drag the numbered points to steer.
Settings apply immediately. The TX drawer contains RF settings, calibration
and Start/Stop. Recalibrate after changing frequency or replay length.
Closing the app mutes all boards.

I/Q uploads accept complex `.npy` arrays or two-column I,Q text/CSV, with exactly
the selected sample count. Each beam can use a different file. The reference
monitor runs automatically, but cannot see backwards through an amplified
REF splitter.

For the Python API and reusable Qt components, see
[IQ Transmission](../../docs/iq-transmission.rst) and
[Application Framework](../../docs/application-framework.rst).
