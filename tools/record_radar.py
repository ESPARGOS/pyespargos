#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[1]))

import numpy as np
import espargos
import time
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("host", help="ESPARGOS controller host/IP")
parser.add_argument("-o", "--output", default="/tmp/espargos_radar_recording.npz", help="Output NPZ filename")
parser.add_argument("-d", "--duration", type=float, default=20.0, help="Capture duration in seconds")
parser.add_argument("-g", "--gain-calib-duration", type=float, default=1.0, help="Duration over which packets are collected for gain calibration, in seconds")
args = parser.parse_args()

pool = espargos.Pool([espargos.Board(args.host)])

# Pool-wide radar setup: transmit from a single antenna, receive L-LTF CSI. CFO
# compensation is disabled by default (TX and RX share a reference clock).
session = espargos.radar.RadarSession(
    pool,
    espargos.radar.RadarConfig(
        tx_antennas=2,
        interval=0.01,
        format="lltf",
        tx_power=espargos.csi.wifi_tx_power_t.WIFI_TX_POWER_2_DBM,
    ),
)

csi_count = 0
start_time = 0
timestamp_records = []
tx_timestamp_records = []
csi_lltf_records = []

calibration = pool.get_calibration()
if calibration is not None:
    channel_primary = calibration.channel_primary
else:
    wificonf = pool.get_wificonf()
    channel_primary = int(wificonf.get("channel-primary", 1))

frequencies = espargos.util.get_frequencies_lltf(channel_primary)
center = espargos.util.get_center_frequency(channel_primary)
subcarrier_frequencies = frequencies - center


def on_new_csi(cluster: espargos.CSICluster):
    global csi_count

    csi_count = csi_count + 1
    print(f"Collected {csi_count} packets with CSI at rate {csi_count / (time.monotonic() - start_time):.2f} pkt/s", end="\r")

    csi_lltf = cluster.deserialize_csi_lltf()
    csi_lltf = pool.get_calibration().apply_lltf(csi_lltf)

    tx_timestamp_s = cluster.get_radar_tx_info().get_hardware_tx_timestamp_ns() / 1e9
    corrected = espargos.radar.correct_radar_csi_tx_timestamps(
        csi_lltf[np.newaxis, ...],
        np.asarray([tx_timestamp_s], dtype=np.float64),
        np.asarray([cluster.get_radar_tx_index()], dtype=np.int32),
        subcarrier_frequencies,
        calibration.sensor_clock_offsets,
        tx_timestamp_offset_s=session.tx_timestamp_offset_s,
    )[0]

    timestamp_records.append(cluster.get_sensor_timestamps())
    tx_timestamp_records.append(tx_timestamp_s)
    csi_lltf_records.append(corrected)


try:
    # Use automatic gain control while receiving the calibration signal
    pool.set_gain_settings({"rx_gain_enable": False, "fft_scale_enable": False})
    pool.start()
    pool.calibrate(per_board=False, duration=2)
    calibration = pool.get_calibration()

    # Configure radar TX/RX and lock in per-sensor gains before the main capture
    session.configure()
    session.calibrate_gains(duration=args.gain_calib_duration, drive=True)

    # Start main capture loop with CSI callback
    pool.add_csi_callback(on_new_csi, cb_predicate=session.predicate("all"))

    start_time = time.monotonic()
    while time.monotonic() < start_time + args.duration:
        pool.run()
finally:
    try:
        session.stop()
    except Exception:
        pass
    pool.stop()
    output_path = pathlib.Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        output_path,
        timestamps=np.asarray(timestamp_records),
        tx_timestamps=np.asarray(tx_timestamp_records),
        csi_lltf=np.asarray(csi_lltf_records),
    )
    print(f"Saved {len(csi_lltf_records)} CSI packets to {output_path}")
