#!/usr/bin/env python3
"""Repeated real-array broadband sync/calibration and settings-restoration test.

Run with the checkout on PYTHONPATH, or directly from tools/. Requires API 4
and the corresponding sensor firmware. Saves timings and failures as JSON.
"""

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import espargos
from espargos import iq_reference


def verify_tones(pool, expected):
    """Independent CW checks of the broadband result after settings restore.

    Tones are test stimuli only; synchronization/calibration never use them.
    Measure the actual carrier frequency rather than assuming the requested
    transmitter setting is exact.
    """
    board = pool.master_board
    fs = pool.calibration_sample_rate
    center = pool.calibration_center_frequency
    row, col = board.revision.antenna_id_to_row_col(0)
    ref = row * 4 + col
    cal = pool.calibration
    errors = []
    try:
        pool.apply_config({"gain_mode": "manual", "rx_gain": 40, "rf_switch": 1, "trigger": {"mode": "interval", "interval_chunks": max(128, 2 ** round(np.log2(fs / 256 / 100))), "burst_chunks": 4}})
        for offset in (-min(fs * 0.15, 12e6), min(fs * 0.30, 12e6)):
            board.iq.set_reference("cw", center + offset, 50)
            clusters = iq_reference.collect(pool, count=24)
            if not all(pool.calibration_applies_to(c) for c in clusters):
                raise AssertionError("Calibration was invalidated by settings restoration")
            x = np.stack([c.iq.reshape(-1, 256) for c in clusters])
            x -= x.mean(axis=-1, keepdims=True)
            carrier = np.angle(np.sum(x[:, ref, 1:] * np.conj(x[:, ref, :-1]), axis=-1)) * fs / (2 * np.pi)
            cross = np.mean(x * np.conj(x[:, ref : ref + 1]), axis=-1)
            measured = np.angle(cross)
            predicted = cal.phase_offsets.reshape(-1) - 2 * np.pi * carrier[:, None] * cal.timing_offsets.reshape(-1)
            residual = np.angle(np.exp(1j * (measured - predicted)))
            errors.append(float(np.max(np.abs(np.mean(residual, axis=0)))))
        if max(errors) > 0.15:
            raise AssertionError(f"Independent CW phase residual {max(errors):.3f} rad")
        return errors
    finally:
        board.iq.set_reference()
        board.iq.set_config({key: expected[key] for key in ("receivers", "rf_switch", "trigger")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--frequencies-mhz", type=float, nargs="+", help="Sweep receiver/calibration center frequencies (defaults to current setting)")
    parser.add_argument("--decimations", type=int, nargs="+", default=[1, 2, 4, 6, 8, 10])
    parser.add_argument("--output", default="/tmp/iq-reference-results.json")
    parser.add_argument("--verify-tones", action="store_true")
    parser.add_argument("--gain-mode", choices=["manual", "auto"], default="manual")
    parser.add_argument("--gain-index", type=int, default=40)
    parser.add_argument("--trigger-mode", choices=["interval", "accumulate", "signal"], default="interval")
    args = parser.parse_args()
    board = espargos.Board(args.host)
    board.start(transports=["websocket"])
    pool = espargos.IQReceiver([board])
    pool.start_processing()
    saved = board.iq.get_config()
    results = []
    try:
        for frequency in args.frequencies_mhz or [saved["receivers"][0]["rf_freq_hz"] / 1e6]:
            pool.apply_config({"mode": "iq", "rf_freq_hz": round(frequency * 1e6)})
            for decimation in args.decimations:
                trigger = {"mode": args.trigger_mode}
                pool.apply_config({"mode": "iq", "adc_decimation": decimation, "gain_mode": args.gain_mode, "rx_gain": args.gain_index, "rf_switch": 2, "trigger": trigger})
                expected = board.iq.get_config()
                for cycle in range(args.cycles):
                    result = {"frequency_mhz": frequency, "decimation": decimation, "cycle": cycle}
                    try:
                        start = time.monotonic()
                        pool.sync()
                        result["sync_seconds"] = time.monotonic() - start
                        actual = board.iq.get_config()
                        for key in ("receivers", "rf_switch", "adc_decimation", "trigger"):
                            if expected[key] != actual[key]:
                                raise AssertionError(f"{key} was not restored")
                        if args.verify_tones:
                            result["tone_residual_radians"] = verify_tones(pool, expected)
                        result["ok"] = True
                    except Exception as error:
                        result.update(ok=False, error=str(error))
                    results.append(result)
                    Path(args.output).write_text(json.dumps(results, indent=2) + "\n")
                    print(json.dumps(result), flush=True)
    finally:
        try:
            board.iq.set_reference()
            board.iq.set_config({key: saved[key] for key in ("mode", "receivers", "rf_switch", "adc_decimation", "trigger")})
        finally:
            pool.close()
            board.close()
    if not all(result["ok"] for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
