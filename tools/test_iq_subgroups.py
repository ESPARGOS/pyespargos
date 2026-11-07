"""Exercise subgroup calibration on one array (changes its configuration).

Run with PYTHONPATH=. python tools/test_iq_subgroups.py HOST.
Uses wired REF pilots at 2385 MHz and leaves transmission muted on exit.
"""

import argparse
import time
from espargos import Pool
from espargos.waveforms import CW
from espargos.calibration import Antenna, CalibrationState
from espargos._iq_calibration.capture import _Captures

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("host")
host = parser.parse_args().host
with Pool(host) as pool:
    pool.configure(mode="iq_rx", frequency_hz=2385e6, sample_rate_hz=80e6, output="reference", rx_gain=30)
    pool.boards[0].iq.set_config({"trigger": {"mode": "interval", "interval_chunks": 8192, "burst_chunks": 1}})
    print("INITIAL RX", flush=True)
    rx = pool.calibrate()
    print("RX PASS", len(rx.groups[0].corrections), flush=True)
    # A new owner starts independent groups on the established hardware grid.
with Pool(host) as pool:
    selected = pool.select(antennas=[0, 1, 2, 3])
    selected.configure(mode="iq_tx", frequency_hz=2385e6, output="reference", rf_gain={"a": 0, "b": 0}, bandwidth_mhz=60)
    tx = pool.tx
    with tx.update(samples=256):
        tx.signals.add(waveform=CW(offset_hz=1.25e6), amplitude=0.05, antennas=selected)
    pool.select(antennas=[0, 1, 4, 5]).calibrate(tx_method="monitor")
    pool.select(antennas=[2, 3, 6, 7]).calibrate(tx_method="monitor")
    b = pool.boards[0]
    before = b.iq.get_config()
    saved_groups = pool.calibrations.groups
    try:
        pool.select(antennas=[0, 2, 4, 6]).calibrate()
    except ValueError as error:
        assert "complete calibration group" in str(error)
    else:
        raise AssertionError("Partial overlap was accepted")
    assert b.iq.get_config() == before
    assert pool.calibrations.groups == saved_groups
    print("PASS partial overlap rejected without hardware changes", flush=True)
    pool.select(antennas=[2, 3]).start_transmission()
    for method in ["monitor", "reciprocity"]:
        b = pool.boards[0]
        old_tx = b.control.get_json("get_iq_tx")
        old_rx = b.iq.get_config()
        untouched = pool.calibrations.select(antennas=[Antenna(host, a) for a in [2, 3, 6, 7]])
        started = time.monotonic()
        print("SUBGROUP", method, flush=True)
        result = pool.select(antennas=[0, 1, 4, 5]).calibrate(tx_method=method)
        new_tx = b.control.get_json("get_iq_tx")
        new_rx = b.iq.get_config()
        assert old_rx["modes"] == new_rx["modes"]
        assert not old_tx["muted"] and not new_tx["muted"]
        for a in [2, 3, 6, 7]:
            assert old_rx["receivers"][a] == new_rx["receivers"][a], (method, a, "RX settings changed")
            for key in ["frequency_hz", "origin_ns", "epoch", "samples", "output", "rf_gain", "bandwidth_mhz", "amplitude"]:
                assert old_tx["sensors"][a].get(key) == new_tx["sensors"][a].get(key), (method, a, key)
            for key in ["phase_deg", "delay_samples"]:
                assert old_tx["calibration"][key][a] == new_tx["calibration"][key][a], (method, a, key)
        assert pool.calibrations.select(antennas=[Antenna(host, a) for a in [2, 3, 6, 7]]).groups == untouched.groups
        # Validate retained RX corrections against fresh packets, not cached state.
        capture = _Captures(pool.boards)
        try:
            frames = capture.collect([6, 7], 256, 2, 5, {a: new_rx["config_generation"] for a in [6, 7]})
            for a, packets in frames.items():
                state = CalibrationState.from_iq_packet(host, a, packets[-1][0], new_tx["sensors"][a]["boot_id"])
                untouched.correction(state)
        finally:
            capture.close()
        assert {c.state.antenna.antid for g in result.groups for c in g.corrections} == {0, 1, 4, 5}
        print("PASS", method, round(time.monotonic() - started, 2), result.diagnostics, flush=True)
    print("RX SUBGROUP", flush=True)
    result = pool.select(antennas=[6, 7]).calibrate()
    print("PASS RX", len(result.groups[0].corrections), flush=True)
    try:
        pool.select(antennas=[0]).calibrate(tx_method="reciprocity")
    except ValueError as e:
        print("PASS single TX rejected:", e, flush=True)
    else:
        raise AssertionError("single TX reciprocity accepted")
