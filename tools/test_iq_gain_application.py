#!/usr/bin/env python3
"""Check the first raw ADC gain codes after live Bluetooth IQ settings changes.

Runs on one eight-sensor array and restores its receive configuration on exit.
The per-sample gain bits are stamped by the radio, independently of the
generation acknowledgement and the configured gain returned by the controller.
"""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
import threading

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from espargos import Board


def run(board, rates, iterations):
    iq = board.iq
    saved = iq.get_config()
    if not all(mode == "iq_rx" for mode in saved["modes"]):
        raise ValueError("Select IQ RX on all eight sensors before running the gain check")
    condition = threading.Condition()
    records = defaultdict(lambda: defaultdict(list))

    def observe(message):
        packet = message.payload
        with condition:
            chunks = records[packet.config_generation][message.antenna_id]
            if len(chunks) < 4:
                gains = np.unique((packet.samples >> 20) & 127).tolist()
                chunks.append((packet.chunk_counter, gains))
                condition.notify_all()

    handle = iq.subscribe_chunks(observe)
    try:
        for rate in rates:
            iq.set_config_and_wait(
                {
                    "capture_path": "bluetooth",
                    "bluetooth_sample_rate_hz": rate,
                    "dc_tracking": False,
                    "trigger": {"mode": "interval", "interval_chunks": 128, "burst_chunks": 12},
                }
            )
            for step in range(iterations):
                gains = [(step * 19 + antenna * 13) % 77 for antenna in range(8)]
                receivers = [dict(antid=a, gain_mode="manual", rx_gain=gain) for a, gain in enumerate(gains)]
                generation = iq.set_config_and_wait({"receivers": receivers})
                with condition:
                    current = records[generation]
                    complete = condition.wait_for(lambda: all(len(current[a]) >= 4 for a in range(8)), timeout=3)
                    wrong = {a: [(chunk, codes) for chunk, codes in current[a] if codes != [gain]] for a, gain in enumerate(gains)}
                    missing = [a for a in range(8) if len(current[a]) < 4]
                    records.clear()
                wrong = {a: chunks for a, chunks in wrong.items() if chunks}
                result = {"rate": rate, "generation": generation, "expected": gains, "missing": missing, "wrong": wrong}
                print(json.dumps(result), flush=True)
                if wrong or not complete:
                    raise AssertionError(f"ADC gain check failed at generation {generation}")
    finally:
        board.unsubscribe_sensor_messages(handle)
        iq.set_config_and_wait({key: saved[key] for key in ("capture_path", "bluetooth_sample_rate_hz", "receivers", "dc_tracking", "trigger")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--transport", choices=("udp", "websocket"), default="udp")
    parser.add_argument("--rates", nargs="+", type=int, default=[1600000, 32000000], choices=(1600000, 3200000, 4000000, 8000000, 16000000, 32000000))
    parser.add_argument("--iterations", type=int, default=16)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    board = Board(args.host)
    try:
        board.start(transports=[args.transport])
        run(board, args.rates, args.iterations)
    finally:
        board.close()


if __name__ == "__main__":
    main()
