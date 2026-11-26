# IQ Signal Analyzer

> [!WARNING]
> Continuous IQ receive streaming is experimental and under development.
> The associated demos are unstable, and the API will change without notice.

View the time trace, spectrum, waterfall and relative antenna phase from one or more ESPARGOS arrays.

```sh
python demos/iq/signal-analyzer/main.py <host>[,<host>...]
```

To follow a narrowband signal continuously, open the RX controls, select **Digital downsampling** as the trigger mode, and choose an **Output rate**. This requires RX streaming firmware on the controller and sensors.
