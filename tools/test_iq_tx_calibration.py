#!/usr/bin/env python
"""Hardware test for controller-monitor and reciprocal TX calibration."""

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
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
    parser.add_argument("--method", choices=("monitor", "reciprocity"), default="monitor")
    parser.add_argument("--path", choices=("reference", "antenna_l", "antenna_r"), default="reference")
    args = parser.parse_args()
    pool = espargos.Pool([host.strip() for host in args.hosts.split(",")])
    boards, success = [], False
    try:
        pool.open(transports=["websocket"])
        boards = pool.boards
        pool.configure(mode="iq_rx", frequency_hz=round(args.frequency * 1e6))
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
            pool.select(boards=board.host).configure(mode="iq_tx", frequency_hz=round(args.frequency * 1e6))
            wait(board, lambda s: all(x["enabled"] and x["applied_mode"] == "iq_tx" for x in s["sensors"]))
        began = time.monotonic()
        result = pool.calibrate(tx_method=args.method, path=args.path)
        elapsed = time.monotonic() - began
        for group in result.select(direction="tx").groups:
            for correction in group.corrections:
                state = correction.state
                print(f"{state.antenna.host} / {state.antenna.antid}: " f"{correction.phase_rad * 180 / 3.141592653589793:.2f} degrees, " f"{correction.delay_s * state.sample_rate_hz:.4f} samples")
        print(f"Validated {args.method} calibration: {result.diagnostics}")
        print(f"Calibration completed in {elapsed:.2f} seconds.")
        print("TX is calibrated and silent. Use Start on the IQ Transmission page to resume.")
        success = True
    finally:
        for board in boards:
            try:
                post(board, "set_iq_tx", {"muted": True, **({} if success else {"modes": ["iq_rx"] * 8})})
            except Exception as error:
                print(f"Could not stop transmission on {board.host}: {error}", file=sys.stderr)
        pool.close()


if __name__ == "__main__":
    main()
