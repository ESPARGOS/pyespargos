# IQ Signal Analyzer

View the time trace, spectrum, waterfall and relative antenna phase from one or more ESPARGOS arrays.

```sh
python demos/iq/signal-analyzer/main.py <host>[,<host>...]
```

To follow a narrowband signal continuously, add `--stream`. The default is 16 kSa/s per antenna. Use `--stream 32000`, `--stream 64000`, `--stream 80000` or `--stream 100000` for a wider band. This option requires RX streaming firmware on the controller and sensors.
