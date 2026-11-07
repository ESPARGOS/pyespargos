#!/usr/bin/env python3
"""Save read-only board diagnostics and optionally check restored settings."""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import espargos

p = argparse.ArgumentParser()
p.add_argument("host")
p.add_argument("output")
p.add_argument("--compare")
a = p.parse_args()
b = espargos.Board(a.host)
r = b.wifi_rx
settings = dict(wifi=r.get_config(), cfo=r.get_cfo_correction(), gain=r.get_gain_settings(), acquire=r.get_csi_acquisition_config(), tx=b.wifi_tx.get_config(), rf=int(r.get_rf_switch()), mac=r.get_mac_filter(), overrides=r.get_channel_overrides())
result = {"settings": settings, "debug": b.control.get_json("get_sensor_debug")}
if a.compare:
    original = json.loads(pathlib.Path(a.compare).read_text())
    result["restoration_differences"] = {k: dict(original=original[k], current=settings[k]) for k in original if original[k] != settings[k]}
pathlib.Path(a.output).write_text(json.dumps(result, indent=2))
print(
    json.dumps(
        {
            "saved": a.output,
            "restoration_differences": result.get("restoration_differences"),
            "controller_uptime_ms": result["debug"].get("controller_uptime_ms"),
            "sensors": [{k: s[k] for k in ["antid", "uptime_ms", "wifi_csi_cb", "csi_watchdog_reinits"]} for s in result["debug"]["sensors"]],
        },
        indent=2,
    )
)
