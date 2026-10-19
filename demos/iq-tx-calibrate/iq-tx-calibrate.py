#!/usr/bin/env python
"""Calibrate a complete TX array, including an amplified Aperture Kit."""

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import espargos


def post(board, command, payload):
    return board.control.post_json(command, payload)


def wait(board, predicate):
    deadline = time.monotonic() + 10
    while True:
        state = board.control.get_json("get_iq_tx")
        if predicate(state):
            return state
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{board.host}: sensor settings were not acknowledged")
        time.sleep(0.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", help="Comma-separated controller addresses sharing one REF network")
    parser.add_argument("--frequency", required=True, type=float, metavar="MHZ", help="Calibration and TX center frequency in MHz")
    parser.add_argument("--rx-gain", type=int, help="Override calibration receiver gain for weak or clipping paths")
    route = parser.add_mutually_exclusive_group()
    route.add_argument("--pilot-output", choices=("reference", "antenna_l", "antenna_r"), default="reference", help="Reciprocal pilot path (default: reference)")
    route.add_argument("--over-the-air", dest="pilot_output", action="store_const", const="antenna_l", help="Send pilots through the left antennas instead of REF")
    args = parser.parse_args()
    boards, pool, success = [], None, False
    try:
        for host in args.hosts.split(","):
            board = espargos.Board(host.strip())
            boards.append(board)
            post(board, "set_iq_tx", {"muted": True, "modes": ["iq_rx"] * 8, "frequency_hz": round(args.frequency * 1e6)})
            wait(board, lambda s: all(x["applied_mode"] == "iq_rx" for x in s["sensors"]))
        states = [b.control.get_json("get_iq_tx") for b in boards]
        lengths = {s["samples"] for state in states for s in state["sensors"] if s["samples"]}
        if len(lengths) > 1:
            raise ValueError("Choose the same waveform sample count on all boards before calibrating")
        length = next(iter(lengths), 1024)
        for board, state in zip(boards, states):
            missing = [a for a, sensor in enumerate(state["sensors"]) if not sensor["samples"]]
            if missing:
                # A new TX session needs a source to restore after the pilots.
                reply = json.loads(
                    post(
                        board,
                        "set_iq_tx_waveform",
                        {
                            "type": "cw",
                            "samples": length,
                            "amplitude": 0.05,
                            "frequency_hz": 1250000,
                            "antennas": missing,
                        },
                    )
                )
                ready = wait(board, lambda s: all(s["sensors"][a]["generation"] == reply["generation"] for a in missing))
                if any(ready["sensors"][a]["error"] for a in missing):
                    raise RuntimeError(f"{board.host}: could not prepare a TX waveform")
            post(board, "set_iq_tx", {"modes": ["iq_tx"] * 8})
            wait(board, lambda s: all(x["enabled"] and x["applied_mode"] == "iq_tx" for x in s["sensors"]))
        pool = espargos.IQPool(boards)
        pool.start()
        began = time.monotonic()
        result = pool.calibrate_tx(pilot_output=args.pilot_output, rx_gain=args.rx_gain)
        elapsed = time.monotonic() - began
        for board, calibration in zip(boards, result["calibration"]):
            print(f"\n{board.host}\nSensor   Phase (degrees)   Delay (samples)")
            for a, (phase, delay) in enumerate(zip(calibration["phase_deg"], calibration["delay_samples"])):
                print(f"{a:6d} {phase:17.2f} {delay:17.4f}")
        print(f"\nValidated {result['links']} reciprocal links: {result['phase_residual_deg']:.2f} degrees, " f"{result['delay_residual_samples']:.4f} samples RMS.")
        print(f"Calibration completed in {elapsed:.2f} seconds.")
        print("TX is calibrated and silent. Use Start on the IQ Transmission page to resume.")
        success = True
    finally:
        for board in boards:
            try:
                post(board, "set_iq_tx", {"muted": True, **({} if success else {"modes": ["iq_rx"] * 8})})
            except Exception as error:
                print(f"Could not stop transmission on {board.host}: {error}", file=sys.stderr)
        if pool:
            pool.close()
        for board in boards:
            board.close()


if __name__ == "__main__":
    main()
