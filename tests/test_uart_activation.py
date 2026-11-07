"""Exercise console-to-framed transport activation after a baud-rate probe."""

import unittest
from unittest.mock import patch

from espargos.uart import UARTClient


class Console:
    def __init__(self, partial_line):
        self.line = bytearray(partial_line)
        self.active_baud = None
        self.baudrate = None

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def flush(self):
        pass

    def write(self, data):
        for byte in data:
            self.line.append(byte)
            if byte == 10:
                prefix = b"ESPARGOS-UART-MODE:"
                if self.line.startswith(prefix):
                    self.active_baud = int(self.line[len(prefix):])
                self.line.clear()


class UARTActivationTests(unittest.TestCase):
    def test_activation_after_probe_or_clean_console(self):
        for partial in (b"", b"\x13probe-at-wrong-baud"):
            for baud in (2_000_000, 3_000_000):
                with self.subTest(partial=partial, baud=baud):
                    client = UARTClient.__new__(UARTClient)
                    client._serial = Console(partial)
                    client.boot_baudrate = 115200
                    client.baudrate = baud
                    client.activation_retry_interval = .05

                    def hello():
                        self.assertEqual(client._serial.active_baud, baud)
                        self.assertEqual(client._serial.baudrate, baud)

                    client.hello = hello
                    with patch("espargos.uart.time.sleep"):
                        client._activate_transport_mode()


if __name__ == "__main__":
    unittest.main()
