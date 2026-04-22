#!/usr/bin/env python

import argparse
import pathlib
import sys
import threading
import time

import numpy as np
import PyQt6.QtCore

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import espargos
import espargos.constants
from demos.common import ESPARGOSApplication


class EspargosDemoRadarScheduleCheck(ESPARGOSApplication):
    radarResidualsChanged = PyQt6.QtCore.pyqtSignal()
    latestSourceChanged = PyQt6.QtCore.pyqtSignal()
    packetCountChanged = PyQt6.QtCore.pyqtSignal()
    statusTextChanged = PyQt6.QtCore.pyqtSignal()
    sensorCountChanged = PyQt6.QtCore.pyqtSignal()
    scheduleOffsetChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "period_us": 80000,
        "start_us": 10000,
        "slot_us": 10000,
        "history_packets": 100,
        "tx_power": 34,
        "tx_phymode": 2,
        "tx_rate": 11,
        "rfswitch_state": 2,
    }

    def __init__(self, argv):
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Verify radar TX schedule timing using received CSI timestamps",
            add_help=False,
        )
        super().__init__(argv, argparse_parent=parser)

        self._residuals_us = np.full(self.get_initial_config("pool", "hosts", default=[]).__len__() * 8 or 8, np.nan, dtype=np.float64)
        self._residual_std_us = np.full_like(self._residuals_us, np.nan, dtype=np.float64)
        self._latest_source = "No radar packet received yet"
        self._packet_count = 0
        self._status_text = "Waiting for initialization"
        self._radar_schedule_by_source_mac = {}
        self._packet_schedule_offsets_us = []
        self._schedule_offset_us = np.nan
        self._residual_history_us = [[] for _ in range(len(self._residuals_us))]
        self._run_worker_enabled = True
        self._run_worker_thread = None

        self.initialize_pool(calibrate=False)
        self.initComplete.connect(self.applyRadarSchedule)
        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "radar-schedule-check-ui.qml")

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)
        self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})
        self.pool.calibrate(duration=2, per_board=False, run_in_thread=True)
        self._residuals_us = np.full(np.prod(self.pool.get_shape()), np.nan, dtype=np.float64)
        self.sensorCountChanged.emit()
        self.pool.add_csi_callback(
            self._on_csi_cluster,
            cb_predicate=lambda completion, age: np.sum(completion) >= np.prod(completion.shape) - 1,
        )
        self.pooldrawer.calibrationStarted.connect(self._on_calibration_started)
        self.pooldrawer.calibrationFinished.connect(self._on_calibration_finished)
        self._ensure_run_worker()

    def _ensure_run_worker(self):
        if self._run_worker_thread is not None and self._run_worker_thread.is_alive():
            return

        def _run_worker():
            while self._run_worker_enabled:
                try:
                    self.pool.run()
                except Exception:
                    time.sleep(0.05)

        self._run_worker_thread = threading.Thread(target=_run_worker, daemon=True)
        self._run_worker_thread.start()

    def _reset_packet_state(self):
        self._residuals_us[:] = np.nan
        self._residual_std_us[:] = np.nan
        self._residual_history_us = [[] for _ in range(len(self._residuals_us))]
        self._latest_source = "No radar packet received yet"
        self._packet_count = 0
        self._packet_schedule_offsets_us.clear()
        self._schedule_offset_us = np.nan
        self.radarResidualsChanged.emit()
        self.latestSourceChanged.emit()
        self.packetCountChanged.emit()
        self.scheduleOffsetChanged.emit()

    @PyQt6.QtCore.pyqtSlot()
    def _on_calibration_started(self):
        self.disableRadarSchedule(update_status=False)
        self._reset_packet_state()
        self._status_text = "Calibration running"
        self.statusTextChanged.emit()

    @PyQt6.QtCore.pyqtSlot(bool, str)
    def _on_calibration_finished(self, success: bool, error_message: str):
        if not success:
            self._status_text = f"Calibration failed: {error_message}" if error_message else "Calibration failed"
            self.statusTextChanged.emit()
            return

        self._status_text = "Calibration finished, re-applying radar schedule"
        self.statusTextChanged.emit()
        self.applyRadarSchedule()

    @staticmethod
    def _normalize_mac(mac: str) -> str:
        return str(mac).replace(":", "").lower()

    @staticmethod
    def _antid_to_row_col(board_revision, antid: int) -> tuple[int, int]:
        esp_num = board_revision.antid_to_esp_num[antid]
        return board_revision.esp_num_to_row_col(esp_num)

    def _build_schedule_lookup(self, pool_radar_config: espargos.radar.RadarPoolConfig):
        schedule_by_source_mac = {}
        calibration = self.pool.get_calibration()
        for board_index, (board_obj, board_config) in enumerate(zip(self.pool.boards, pool_radar_config.board_configs)):
            for antid, mac in enumerate(board_config["mac_by_antid"]):
                row, col = self._antid_to_row_col(board_obj.revision, antid)
                reference_start_s = board_config["start_by_antid"][antid] / espargos.radar.RADAR_TIME_SCALE - calibration.sensor_clock_offsets[board_index, row, col]
                schedule_by_source_mac[self._normalize_mac(mac)] = {
                    "board_index": board_index,
                    "antid": antid,
                    "reference_start_s": reference_start_s,
                    "period_s": board_config["period_by_antid"][antid] / espargos.radar.RADAR_TIME_SCALE,
                }
        self._radar_schedule_by_source_mac = schedule_by_source_mac

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=sensorCountChanged)
    def sensorCount(self):
        return int(np.prod(self.pool.get_shape())) if hasattr(self, "pool") else 8

    @PyQt6.QtCore.pyqtProperty(list, constant=False, notify=radarResidualsChanged)
    def radarResidualTexts(self):
        texts = []
        for mean_value, std_value in zip(self._residuals_us, self._residual_std_us):
            if not np.isfinite(mean_value):
                texts.append("mu      n/a us\nsd      n/a us")
            else:
                texts.append(f"mu {mean_value:+10.4f} us\nsd {std_value:10.4f} us")
        return texts

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=latestSourceChanged)
    def latestSource(self):
        return self._latest_source

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=packetCountChanged)
    def packetCount(self):
        return self._packet_count

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=statusTextChanged)
    def statusText(self):
        return self._status_text

    @PyQt6.QtCore.pyqtProperty(float, constant=False, notify=scheduleOffsetChanged)
    def scheduleOffsetUs(self):
        return float(self._schedule_offset_us) if np.isfinite(self._schedule_offset_us) else 0.0

    @PyQt6.QtCore.pyqtSlot()
    def applyRadarSchedule(self):
        self._ensure_run_worker()
        if len(self.pool.boards) != 1:
            self._status_text = "Radar schedule check demo currently supports exactly one ESPARGOS board"
            self.statusTextChanged.emit()
            return

        calibration = self.pool.get_calibration()
        if calibration is None:
            self._status_text = "Calibration required before radar schedule can be applied"
            self.statusTextChanged.emit()
            return

        active_by_antid = [True] * espargos.constants.ANTENNAS_PER_BOARD
        requested_start_s = float(self.appconfig.get("start_us")) / 1e6
        min_safe_start_s = max(0.0, -float(np.nanmin(calibration.sensor_clock_offsets))) + 1e-6
        effective_start_s = max(requested_start_s, min_safe_start_s)
        if effective_start_s != requested_start_s:
            self.appconfig.set({"start_us": int(np.ceil(effective_start_s * 1e6))})

        slot_s = float(self.appconfig.get("slot_us")) / 1e6
        t0_by_antid = effective_start_s + np.arange(espargos.constants.ANTENNAS_PER_BOARD, dtype=np.float64) * slot_s
        period_by_antid = np.full(espargos.constants.ANTENNAS_PER_BOARD, float(self.appconfig.get("period_us")) / 1e6, dtype=np.float64)
        current_radar_config = self.pool.get_radar_config()

        try:
            pool_radar_config = espargos.radar.build_pool_config(
                calibration=calibration,
                active_by_antid=active_by_antid,
                t0_by_antid=t0_by_antid,
                period_by_antid=period_by_antid,
                tx_power=int(self.appconfig.get("tx_power")),
                tx_phymode=int(self.appconfig.get("tx_phymode")),
                tx_rate=int(self.appconfig.get("tx_rate")),
                rfswitch_state=int(self.appconfig.get("rfswitch_state")),
                mac_by_antid=current_radar_config.get("mac_by_antid"),
            )
            self.pool.set_radar_config(pool_radar_config)
        except Exception as exc:
            self._status_text = f"Failed to apply radar schedule: {exc}"
            self.statusTextChanged.emit()
            return

        self._build_schedule_lookup(pool_radar_config)
        self._reset_packet_state()
        self._status_text = "Applied radar schedule for all 8 transmitters"
        self.statusTextChanged.emit()

    @PyQt6.QtCore.pyqtSlot()
    def disableRadarSchedule(self, update_status: bool = True):
        self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})
        self._radar_schedule_by_source_mac = {}
        if update_status:
            self._status_text = "Disabled radar schedule"
            self.statusTextChanged.emit()

    def _on_csi_cluster(self, csi_cluster: espargos.CSICluster):
        if not csi_cluster.is_radar():
            return

        schedule = self._radar_schedule_by_source_mac.get(self._normalize_mac(csi_cluster.get_source_mac()))
        if schedule is None:
            return

        calibration = self.pool.get_calibration()
        timestamps = csi_cluster.get_sensor_timestamps()
        reference_timestamps = timestamps - calibration.sensor_clock_offsets

        expected_reference_time = np.full_like(reference_timestamps, schedule["reference_start_s"], dtype=np.float64)
        if schedule["period_s"] > 0:
            cycle_indices = np.rint((reference_timestamps - schedule["reference_start_s"]) / schedule["period_s"])
            cycle_index = int(np.rint(np.nanmedian(cycle_indices)))
            expected_reference_time = np.full_like(
                reference_timestamps,
                schedule["reference_start_s"] + cycle_index * schedule["period_s"],
                dtype=np.float64,
            )

        raw_residuals_us = (reference_timestamps - expected_reference_time) * 1e6
        raw_residuals_us[~np.isfinite(reference_timestamps)] = np.nan

        packet_offset_us = float(np.nanmedian(raw_residuals_us))
        if np.isfinite(packet_offset_us):
            self._packet_schedule_offsets_us.append(packet_offset_us)
            self._packet_schedule_offsets_us = self._packet_schedule_offsets_us[-128:]
            self._schedule_offset_us = float(np.nanmedian(np.asarray(self._packet_schedule_offsets_us, dtype=np.float64)))

        residuals_us = raw_residuals_us - self._schedule_offset_us if np.isfinite(self._schedule_offset_us) else raw_residuals_us
        residuals_flat_us = residuals_us.reshape(-1)
        history_length = int(np.clip(int(self.appconfig.get("history_packets")), 5, 1000))
        for sensor_index, value in enumerate(residuals_flat_us):
            if not np.isfinite(value):
                continue
            self._residual_history_us[sensor_index].append(float(value))
            if len(self._residual_history_us[sensor_index]) > history_length:
                self._residual_history_us[sensor_index] = self._residual_history_us[sensor_index][-history_length:]

        for sensor_index, history in enumerate(self._residual_history_us):
            if len(history) == 0:
                self._residuals_us[sensor_index] = np.nan
                self._residual_std_us[sensor_index] = np.nan
                continue

            history_array = np.asarray(history, dtype=np.float64)
            self._residuals_us[sensor_index] = float(np.mean(history_array))
            self._residual_std_us[sensor_index] = float(np.std(history_array))

        self._latest_source = f"Latest radar packet: board {schedule['board_index']}, antid {schedule['antid']}"
        self._packet_count += 1
        if np.isfinite(self._schedule_offset_us):
            self._status_text = f"Radar schedule active (common offset {self._schedule_offset_us:+.1f} us, stats over last {history_length} packets)"
        else:
            self._status_text = f"Radar schedule active (stats over last {history_length} packets)"

        self.radarResidualsChanged.emit()
        self.latestSourceChanged.emit()
        self.packetCountChanged.emit()
        self.scheduleOffsetChanged.emit()
        self.statusTextChanged.emit()

    def onAboutToQuit(self):
        self._run_worker_enabled = False
        super().onAboutToQuit()


app = EspargosDemoRadarScheduleCheck(sys.argv)
sys.exit(app.exec())
