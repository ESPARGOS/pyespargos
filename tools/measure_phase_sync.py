#!/usr/bin/env python3
"""Reproducible single-array radar phase-stability capture; restores settings."""

import argparse
import json
import pathlib
import sys
import time
import traceback
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import espargos
from espargos.wifi_tx import RadarSession, RadarSessionConfig


def jsonable(x):
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, np.generic):
        return x.item()
    raise TypeError(type(x).__name__)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("host")
    p.add_argument("--output", required=True)
    p.add_argument("--duration", type=float, default=20)
    p.add_argument("--formats", nargs="+", default=["lltf12", "lltf8", "he20"])
    p.add_argument("--tx", type=int, default=2)
    p.add_argument("--power", type=int, default=8, help="Quarter dBm")
    a = p.parse_args()
    out = pathlib.Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    b = espargos.Board(a.host)
    r = b.wifi_rx
    saved = dict(wifi=r.get_config(), cfo=r.get_cfo_correction(), gain=r.get_gain_settings(), acquire=r.get_csi_acquisition_config(), tx=b.wifi_tx.get_config(), rf=int(r.get_rf_switch()), mac=r.get_mac_filter(), overrides=r.get_channel_overrides())
    (out / "original_settings.json").write_text(json.dumps(saved, indent=2))
    owner = espargos.Pool([b]).open()
    pool = owner.wifi
    pool._gain_phase_enabled = False
    try:
        b.wifi_tx.set_config({"active_by_antid": [False] * 8})
        r.clear_mac_filter()
        r.set_channel_overrides({"override_active": False})
        r.set_cfo_correction(False, 0)
        r.set_gain_settings({"rx_gain_enable": False, "fft_scale_enable": False, "expert_mode_enable": False})
        r.set_csi_acquisition_config({"enable": True, "compress_csi": False, "acquire_csi_force_lltf": False, "lltf_8bit_mode": False})
        r.set_rf_switch(espargos.sensor.RFSwitchState.SENSOR_RFSWITCH_ANTENNA_R)
        b.start()
        pool.calibrate(duration=3)
        for fmt in a.formats:
            r.set_csi_acquisition_config({"acquire_csi_force_lltf": fmt != "he20", "lltf_8bit_mode": fmt == "lltf8"})
            session = RadarSession(pool, RadarSessionConfig(tx_antennas=[a.tx], interval_s=0.01, preamble_format="he20" if fmt == "he20" else "lltf", tx_power=a.power))
            session.configure()
            session.calibrate_gains(duration_s=3, drive_pool=True, settle_s=1)
            metadata = dict(
                host=a.host,
                format=fmt,
                tx=a.tx,
                duration=a.duration,
                unix_start=time.time(),
                settings=dict(cfo=r.get_cfo_correction(), gain=r.get_gain_settings(), acquire=r.get_csi_acquisition_config(), wifi=r.get_config(), tx=b.wifi_tx.get_config()),
                timing_offsets=pool.calibration.timing_offsets,
                sensor_mapping=b.revision.sensor_values_to_antenna_id_list(np.arange(8).reshape(2, 4)),
            )
            metadata["debug_start"] = b.control.get_json("get_sensor_debug")
            records = {k: [] for k in ["csi", "t", "rx_t", "rssi", "cfo", "rx_gain", "fft_gain", "enqueue_us", "tx_count", "clip_fraction", "raw_peak"]}

            def collect(c):
                if c.tx_index != a.tx:
                    return
                if fmt == "he20" and not c.has_he20ltf:
                    return
                z = c.deserialize_csi_he20ltf() if fmt == "he20" else c.deserialize_csi_lltf()
                records["csi"].append(z.reshape(8, -1).astype(np.complex64))
                records["t"].append(c.tx_report.get_hardware_tx_timestamp_ns() / 1e9)
                records["enqueue_us"].append(c.tx_report.software_enqueue_timestamp_us)
                records["tx_count"].append(c.tx_report.tx_count)
                clip = np.full(8, np.nan)
                peak = np.full(8, np.nan)
                for esp in range(8):
                    packet = c.get_csi_packet(0, esp)
                    if packet is None:
                        continue
                    row, col = b.revision.esp_num_to_row_col(esp)
                    if fmt == "lltf12":
                        values = packet._decode_lltf12(52)
                        lo, hi = -2048, 2047
                    elif fmt == "lltf8":
                        values = packet._decode_lltf8().view(np.float32)
                        lo, hi = -128, 127
                    else:
                        values = np.frombuffer(packet.buf[:490], dtype=np.int8).reshape(-1, 2)
                        values = values[np.abs(np.arange(245) - 122) > 1].reshape(-1)
                        lo, hi = -128, 127
                    clip[row * 4 + col] = np.mean(np.abs(values.astype(float)) >= hi)
                    peak[row * 4 + col] = np.max(np.abs(values.astype(float)))
                records["clip_fraction"].append(clip)
                records["raw_peak"].append(peak)
                for name, value in [("rx_t", c.sensor_timestamps), ("rssi", c.rssi), ("cfo", c.cfo), ("rx_gain", c.rx_gain), ("fft_gain", c.fft_gain)]:
                    records[name].append(np.asarray(value).reshape(8).copy())

            cb = pool.add_callback(collect, callback_predicate=session.predicate("all"))
            start = time.monotonic()
            next_log = start
            last_packet_time = start
            last_packet_count = 0
            metadata["capture_end_reason"] = "requested duration"
            try:
                while time.monotonic() - start < a.duration:
                    owner.run(timeout=0.05)
                    if pool.calibration is None:
                        metadata["capture_end_reason"] = "sensor timestamp restart"
                        raise RuntimeError("Sensor timestamp restarted; capture ended to avoid mixing clock epochs")
                    if len(records["t"]) != last_packet_count:
                        last_packet_time = time.monotonic()
                        last_packet_count = len(records["t"])
                    elif time.monotonic() - last_packet_time > 15:
                        metadata["capture_end_reason"] = "no complete radar packets for 15 seconds"
                        break
                    if time.monotonic() >= next_log:
                        print(f'{a.host} {fmt} TX{a.tx}: {len(records["t"])} packets, {time.monotonic()-start:.1f}s', flush=True)
                        next_log += 30
            finally:
                pool.remove_callback(cb)
                metadata["wall_elapsed_s"] = time.monotonic() - start
                metadata["association_stats"] = pool.association_stats
                try:
                    metadata["debug_end"] = b.control.get_json("get_sensor_debug")
                    metadata["settings_end"] = dict(cfo=r.get_cfo_correction(), gain=r.get_gain_settings(), acquire=r.get_csi_acquisition_config(), wifi=r.get_config(), tx=b.wifi_tx.get_config())
                except Exception as exc:
                    metadata["end_readback_error"] = str(exc)
                stem = f"{fmt}_tx{a.tx}"
                np.savez_compressed(out / (stem + ".npz"), **{k: np.asarray(v) for k, v in records.items()})
                (out / (stem + ".json")).write_text(json.dumps(metadata, indent=2, default=jsonable))
                print(f'Saved {out/stem}: {len(records["t"])} packets', flush=True)
    finally:
        operations = [
            lambda: b.wifi_tx.set_config({"active_by_antid": [False] * 8}),
            owner.close,
            lambda: r.set_config(saved["wifi"]),
            lambda: r.set_csi_acquisition_config(saved["acquire"]),
            lambda: b.control.post_json("set_gain_settings", saved["gain"]),
            lambda: r.set_cfo_correction(**saved["cfo"]),
            lambda: r.set_rf_switch(espargos.sensor.RFSwitchState(saved["rf"])),
            lambda: r.set_mac_filter(saved["mac"]),
            lambda: r.set_channel_overrides(saved["overrides"]),
            lambda: b.wifi_tx.set_config(saved["tx"]),
        ]
        failures = []
        for op in operations:
            try:
                op()
            except Exception:
                failures.append(traceback.format_exc())
        (out / "restoration.json").write_text(json.dumps({"errors": failures}, indent=2))
        if failures:
            raise RuntimeError("Settings restoration failed: " + str(failures))
        print(a.host + " settings restored", flush=True)


if __name__ == "__main__":
    main()
