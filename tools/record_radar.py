#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[1]))

import numpy as np
import espargos
import time
import argparse
import warnings

parser = argparse.ArgumentParser()
parser.add_argument("-o", "--output", default="/tmp/espargos_radar_recording.npz", help="Output NPZ filename")
parser.add_argument("-d", "--duration", type=float, default=20.0, help="Capture duration in seconds")
parser.add_argument("-g", "--gain-calib-duration", type=float, default=1.0, help="Duration over which packets are collected for gain calibration, in seconds")
args = parser.parse_args()

pool = espargos.Pool([espargos.Board("192.168.0.223")])

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

rx_gain_records = []
fft_gain_records = []

def collect_gain_settings(cluster : espargos.CSICluster):
    print(f"RX gain: {np.nanmean(cluster.get_rx_gain()):.1f}, FFT gain: {np.nanmean(cluster.get_fft_gain()):.1f}", end="\r")

    rx_gain_records.append(cluster.get_rx_gain())
    fft_gain_records.append(cluster.get_fft_gain())

def on_new_csi(cluster : espargos.CSICluster):
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
        tx_timestamp_offset_s=1085e-9
    )[0]

    timestamp_records.append(cluster.get_sensor_timestamps())
    tx_timestamp_records.append(tx_timestamp_s)
    csi_lltf_records.append(corrected)

def radar_cb_predicate(cluster : espargos.CSICluster):
    return (np.sum(cluster.get_completion()) == 7 and cluster.is_radar() and cluster.has_radar_tx_report())

try:
    pool.set_gain_settings({
        "rx_gain_enable": False,
        "fft_scale_enable": False,
    })    
    pool.start()
    pool.calibrate(per_board=False, duration=2)

    calibration = pool.get_calibration()
    min_safe_start_s = max(0.0, -float(np.nanmin(calibration.sensor_clock_offsets))) + 1e-6
    active_by_sensor = np.zeros((espargos.constants.ROWS_PER_BOARD, espargos.constants.ANTENNAS_PER_ROW), dtype=bool)
    active_by_sensor[0, 2] = True
    radar_config = espargos.radar.build_pool_config(
        calibration,
        active_by_sensor,
        min_safe_start_s,
        0.01,
        espargos.csi.wifi_tx_power_t.WIFI_TX_POWER_2_DBM,
        espargos.csi.wifi_phy_mode_t.WIFI_PHY_MODE_11G,
        espargos.csi.wifi_phy_rate_t.WIFI_PHY_RATE_6M,
        espargos.csi.rfswitch_state_t.SENSOR_RFSWITCH_ANTENNA_R)

    pool.set_radar_config(radar_config)
    collect_gain_callback = pool.add_csi_callback(collect_gain_settings, cb_predicate=radar_cb_predicate)
    gain_calib_start_time = time.monotonic()
    while time.monotonic() < gain_calib_start_time + args.gain_calib_duration:
        pool.run()
    pool.remove_csi_callback(collect_gain_callback)

    # Compute average over all recorded rx and fft gain settings
    if not rx_gain_records or not fft_gain_records:
        raise RuntimeError("No CSI packets collected during gain calibration")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Mean of empty slice", category=RuntimeWarning)
        rx_gain_by_sensor = np.rint(np.nanmean(np.asarray(rx_gain_records, dtype=np.float32), axis=0))
        fft_gain_by_sensor = np.rint(np.nanmean(np.asarray(fft_gain_records, dtype=np.float32), axis=0))
    finite_rx_gains = rx_gain_by_sensor[np.isfinite(rx_gain_by_sensor)]
    finite_fft_gains = fft_gain_by_sensor[np.isfinite(fft_gain_by_sensor)]
    if finite_rx_gains.size == 0 or finite_fft_gains.size == 0:
        raise RuntimeError("No finite gain values collected during gain calibration")
    rx_gain_enable_by_sensor = np.isfinite(rx_gain_by_sensor)
    fft_gain_enable_by_sensor = np.isfinite(fft_gain_by_sensor)
    rx_gain_by_sensor = np.nan_to_num(rx_gain_by_sensor, nan=float(0)).astype(int)
    fft_gain_by_sensor = np.nan_to_num(fft_gain_by_sensor, nan=float(0)).astype(int)
    print(f"Calibrated RX gains by sensor: {rx_gain_by_sensor}, FFT gains by sensor: {fft_gain_by_sensor}")

    pool.set_gain_settings({
        "rx_gain_enable": rx_gain_enable_by_sensor,
        "rx_gain_value": rx_gain_by_sensor,
        "fft_scale_enable": fft_gain_enable_by_sensor,
        "fft_scale_value": fft_gain_by_sensor,
    })

    # Wait for gain settings to take effect
    dump_csi = pool.add_csi_callback(lambda cluster : None, cb_predicate=radar_cb_predicate)
    dump_start_time = time.monotonic()
    while time.monotonic() < dump_start_time + 0.5:
        pool.run()
    pool.remove_csi_callback(dump_csi)

    # Start main capture loop with CSI callback
    pool.add_csi_callback(on_new_csi, cb_predicate=radar_cb_predicate)

    start_time = time.monotonic()
    while time.monotonic() < start_time + args.duration:
        pool.run()
finally:
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
