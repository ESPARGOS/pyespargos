#!/usr/bin/env python

import argparse
import pathlib
import sys
import time

import numpy as np

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

import espargos
import espargos.constants


def normalize_mac(mac: str) -> str:
    return str(mac).replace(":", "").lower()


def antid_to_row_col(board_revision, antid: int) -> tuple[int, int]:
    esp_num = board_revision.antid_to_esp_num[antid]
    return board_revision.esp_num_to_row_col(esp_num)


def build_schedule_lookup(pool: espargos.Pool, pool_radar_config: espargos.radar.RadarPoolConfig):
    calibration = pool.get_calibration()
    schedule_by_source_mac = {}
    for board_index, (board_obj, board_config) in enumerate(zip(pool.boards, pool_radar_config.board_configs)):
        for antid, mac in enumerate(board_config["mac_by_antid"]):
            row, col = antid_to_row_col(board_obj.revision, antid)
            reference_start_s = board_config["start_by_antid"][antid] / espargos.radar.RADAR_TIME_SCALE - calibration.sensor_clock_offsets[board_index, row, col]
            schedule_by_source_mac[normalize_mac(mac)] = {
                "board_index": board_index,
                "antid": antid,
                "reference_start_s": reference_start_s,
                "period_s": board_config["period_by_antid"][antid] / espargos.radar.RADAR_TIME_SCALE,
            }
    return schedule_by_source_mac


def main():
    parser = argparse.ArgumentParser(description="Test whether the configured radar TX schedule matches received CSI timestamps")
    parser.add_argument("host", help="ESPARGOS controller host")
    parser.add_argument("--duration", type=float, default=4.0, help="Capture duration after applying schedule [s]")
    parser.add_argument("--period-us", type=int, default=80000, help="Radar schedule period [us]")
    parser.add_argument("--start-us", type=int, default=10000, help="Reference start time for sensor 0 [us]")
    parser.add_argument("--slot-us", type=int, default=10000, help="Relative slot spacing between sensors [us]")
    parser.add_argument("--threshold-us", type=float, default=50.0, help="Acceptable absolute mean residual threshold [us]")
    args = parser.parse_args()

    pool = espargos.Pool([espargos.Board(args.host)])
    collected = []

    def on_cluster(csi_cluster: espargos.CSICluster):
        if csi_cluster.is_radar():
            collected.append(csi_cluster)

    try:
        pool.start()
        pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})
        pool.calibrate(duration=2, per_board=False, run_in_thread=True)

        calibration = pool.get_calibration()
        if calibration is None:
            raise RuntimeError("Calibration did not produce a CSICalibration object")

        requested_start_s = args.start_us / 1e6
        min_safe_start_s = max(0.0, -float(np.nanmin(calibration.sensor_clock_offsets))) + 1e-6
        effective_start_s = max(requested_start_s, min_safe_start_s)

        t0_by_antid = effective_start_s + np.arange(espargos.constants.ANTENNAS_PER_BOARD, dtype=np.float64) * (args.slot_us / 1e6)
        period_by_antid = np.full(espargos.constants.ANTENNAS_PER_BOARD, args.period_us / 1e6, dtype=np.float64)
        current_radar_config = pool.get_radar_config()

        pool_radar_config = espargos.radar.build_pool_config(
            calibration=calibration,
            active_by_antid=[True] * espargos.constants.ANTENNAS_PER_BOARD,
            t0_by_antid=t0_by_antid,
            period_by_antid=period_by_antid,
            tx_power=int(current_radar_config["tx_power"]),
            tx_phymode=int(current_radar_config["tx_phymode"]),
            tx_rate=int(current_radar_config["tx_rate"]),
            rfswitch_state=int(current_radar_config["rfswitch_state"]),
            mac_by_antid=current_radar_config["mac_by_antid"],
        )
        schedule_by_source_mac = build_schedule_lookup(pool, pool_radar_config)
        pool.set_radar_config(pool_radar_config)

        pool.add_csi_callback(
            on_cluster,
            cb_predicate=lambda cluster: np.sum(cluster.get_completion()) >= np.prod(cluster.get_completion().shape) - 1,
        )

        end_time = time.time() + args.duration
        while time.time() < end_time:
            pool.run()

        if not collected:
            raise RuntimeError("No radar packets were captured")

        residuals_by_antid = {antid: [] for antid in range(espargos.constants.ANTENNAS_PER_BOARD)}
        packet_offsets_us = []
        packet_records = []
        for csi_cluster in collected:
            schedule = schedule_by_source_mac.get(normalize_mac(csi_cluster.get_source_mac()))
            if schedule is None:
                continue

            timestamps = csi_cluster.get_sensor_timestamps()
            reference_timestamps = timestamps - calibration.sensor_clock_offsets
            cycle_indices = np.rint((reference_timestamps - schedule["reference_start_s"]) / schedule["period_s"])
            cycle_index = int(np.rint(np.nanmedian(cycle_indices)))
            expected_reference_time = schedule["reference_start_s"] + cycle_index * schedule["period_s"]
            raw_residuals_us = (reference_timestamps - expected_reference_time) * 1e6
            raw_residuals_us = raw_residuals_us[np.isfinite(raw_residuals_us)]
            if raw_residuals_us.size == 0:
                continue
            packet_offset_us = float(np.nanmedian(raw_residuals_us))
            packet_offsets_us.append(packet_offset_us)
            packet_records.append((schedule["antid"], raw_residuals_us))

        if not packet_records:
            raise RuntimeError("Radar packets were captured, but none produced valid timestamp residuals")

        common_offset_us = float(np.nanmedian(np.asarray(packet_offsets_us, dtype=np.float64)))
        for antid, raw_residuals_us in packet_records:
            residuals_by_antid[antid].extend((raw_residuals_us - common_offset_us).tolist())

        print(f"Captured {len(collected)} radar clusters")
        print(f"Configured start_us={args.start_us}, effective_start_us={int(round(effective_start_s * 1e6))}, slot_us={args.slot_us}, period_us={args.period_us}")
        print(f"Estimated common schedule offset: {common_offset_us:+.3f} us")

        mean_abs_residuals = []
        for antid in range(espargos.constants.ANTENNAS_PER_BOARD):
            values = np.asarray(residuals_by_antid[antid], dtype=np.float64)
            if values.size == 0:
                print(f"TX antid {antid}: no packets")
                continue
            mean = float(np.mean(values))
            std = float(np.std(values))
            mean_abs = float(np.mean(np.abs(values)))
            mean_abs_residuals.append(mean_abs)
            print(f"TX antid {antid}: n={values.size:4d} mean={mean:+8.3f} us std={std:8.3f} us mean|err|={mean_abs:8.3f} us")

        if not mean_abs_residuals:
            raise RuntimeError("Radar packets were captured, but none matched the configured schedule MACs")

        worst_mean_abs = max(mean_abs_residuals)
        print(f"Worst mean absolute residual: {worst_mean_abs:.3f} us")
        if worst_mean_abs > args.threshold_us:
            raise SystemExit(1)
        print("Radar schedule check passed")
    finally:
        try:
            pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})
        except Exception:
            pass
        pool.stop()


if __name__ == "__main__":
    main()
