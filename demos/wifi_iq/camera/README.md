# WiFi IQ camera

From the SDK checkout, run:

```sh
python demos/wifi_iq/camera/main.py -s 192.168.1.2 -o wifi_iq.channel=6 -o wifi_iq.rx_gain=30
```

The camera estimates directions from OFDM packet waveforms on one ESPARGOS board. Configure the board's WiFi reference for generation during calibration. Open **RX** to retune, choose the antenna feed, change the shared trigger or select a transmitter by MAC. Use `--no-camera` for the radio display alone.

See [WiFi IQ reception](../../../docs/wifi-iq-reception.rst) for capture and calibration details and [Shared packet decoders](../../../docs/packet-decoders.rst) for building the decoder from source.
