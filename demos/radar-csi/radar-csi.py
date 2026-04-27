#!/usr/bin/env python

import argparse
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, SingleCSIFormatMixin

import espargos
import espargos.constants
import espargos.util
import numpy as np
import PyQt6.QtCharts
import PyQt6.QtCore


class EspargosDemoRadarCSI(SingleCSIFormatMixin, ESPARGOSApplication):
    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()
    sensorCountChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "period_ms": 16.0,
        "start_ms": 10.0,
        "slot_ms": 10.0,
        "tx_power": 34,
        "tx_phymode": 2,
        "tx_rate": 11,
        "rfswitch_state": 2,
        "tx_timestamp_offset_ns": 1085,
    }

    def __init__(self, argv):
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Show timestamp-corrected radar CSI between TX/RX sensor pairs",
            add_help=False,
        )
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(argv, argparse_parent=parser)

        # Radar CSI is easiest to validate with forced L-LTF. Users can still change
        # this in the pool drawer if they want to experiment with HT formats.
        self.initial_config["pool"]["acquire_lltf_force"] = True

        self.stable_power_minimum = None
        self.stable_power_maximum = None
        self.sensor_count = len(self.get_initial_config("pool", "hosts", default=[])) * espargos.constants.ANTENNAS_PER_BOARD or espargos.constants.ANTENNAS_PER_BOARD
        self._link_rx_indices = np.asarray([], dtype=np.int32)
        self._link_tx_indices = np.asarray([], dtype=np.int32)
        self._link_index_by_rx_tx = np.full((self.sensor_count, self.sensor_count), -1, dtype=np.int32)
        self._subcarrier_range_cache = {}
        self._latest_link_csi = {}
        self._dirty_link_indices = set()
        self._update_link_indices()

        self.initComplete.connect(self.applyRadarSchedule)
        self.initComplete.connect(self.onInitComplete)
        self.initialize_pool(
            calibrate=not self.args.no_calib,
        )
        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "radar-csi-ui.qml")

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)
        self.sensor_count = int(np.prod(self.pool.get_shape()))
        self._update_link_indices()
        self.sensorCountChanged.emit()
        self.pool.add_csi_callback(
            self.onCSI,
            cb_predicate=lambda cluster: cluster.is_radar() and cluster.has_radar_tx_report() and np.any(cluster.get_completion()),
        )

    def onInitComplete(self):
        self.poll_timer = PyQt6.QtCore.QTimer(self)
        self.poll_timer.timeout.connect(self.pollCSI)
        self.poll_timer.start(10)

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=sensorCountChanged)
    def sensorCount(self):
        return self.sensor_count

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=sensorCountChanged)
    def linkCount(self):
        return self.sensor_count * max(0, self.sensor_count - 1)

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=preambleFormatChanged)
    def preambleFormat(self):
        return self.genericconfig.get("preamble_format")

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=preambleFormatChanged)
    def subcarrierCount(self):
        return self._subcarrier_count_for_format(self.genericconfig.get("preamble_format"))

    @PyQt6.QtCore.pyqtSlot(int, result=str)
    def linkName(self, link_index: int):
        rx_index, tx_index = self._link_indices(link_index)
        return f"TX{tx_index:02d} -> RX{rx_index:02d}"

    @PyQt6.QtCore.pyqtSlot()
    def applyRadarSchedule(self):
        if not hasattr(self, "pool") or self.pool.get_calibration() is None:
            return

        calibration = self.pool.get_calibration()
        active_by_antid = [True] * espargos.constants.ANTENNAS_PER_BOARD
        requested_start_s = self._get_schedule_ms("start") / 1e3
        min_safe_start_s = max(0.0, -float(np.nanmin(calibration.sensor_clock_offsets))) + 1e-6
        effective_start_s = max(requested_start_s, min_safe_start_s)
        slot_s = self._get_schedule_ms("slot") / 1e3
        t0_by_antid = effective_start_s + np.arange(espargos.constants.ANTENNAS_PER_BOARD, dtype=np.float64) * slot_s
        period_by_antid = np.full(espargos.constants.ANTENNAS_PER_BOARD, self._get_schedule_ms("period") / 1e3, dtype=np.float64)
        radar_configs = self.pool.get_radar_configs()

        pool_radar_config = espargos.radar.build_pool_config(
            calibration=calibration,
            active_by_antid=active_by_antid,
            t0_by_antid=t0_by_antid,
            period_by_antid=period_by_antid,
            tx_power=int(self.appconfig.get("tx_power")),
            tx_phymode=int(self.appconfig.get("tx_phymode")),
            tx_rate=int(self.appconfig.get("tx_rate")),
            rfswitch_state=int(self.appconfig.get("rfswitch_state")),
            mac_by_antid=[config.get("mac_by_antid") for config in radar_configs],
        )
        self.pool.set_radar_config(pool_radar_config)

    @PyQt6.QtCore.pyqtSlot()
    def disableRadarSchedule(self):
        if hasattr(self, "pool"):
            self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})

    def _subcarrier_count_for_format(self, preamble_format: str) -> int:
        return espargos.csi.get_csi_format_subcarrier_count(preamble_format)

    def _subcarrier_frequencies_for_format(self, preamble_format: str) -> np.ndarray:
        calibration = self.pool.get_calibration()
        if calibration is not None:
            channel_primary = calibration.channel_primary
            channel_secondary = calibration.channel_secondary
        else:
            wificonf = self.pool.get_wificonf()
            channel_primary = int(wificonf.get("channel-primary", 1))
            channel_secondary = int(wificonf.get("channel-secondary", 0))
            channel_secondary = -1 if channel_secondary == 2 else channel_secondary

        if preamble_format == "lltf":
            frequencies = espargos.util.get_frequencies_lltf(channel_primary)
            center = espargos.util.get_center_frequency(channel_primary)
        elif preamble_format == "ht40":
            frequencies = espargos.util.get_frequencies_ht40(channel_primary, channel_secondary)
            center = espargos.util.get_center_frequency(channel_primary, channel_secondary)
        elif preamble_format == "he20":
            frequencies = espargos.util.get_frequencies_he20(channel_primary)
            center = espargos.util.get_center_frequency(channel_primary)
        else:
            frequencies = espargos.util.get_frequencies_ht20(channel_primary)
            center = espargos.util.get_center_frequency(channel_primary)

        return frequencies - center

    def _link_indices(self, link_index: int) -> tuple[int, int]:
        return int(self._link_rx_indices[link_index]), int(self._link_tx_indices[link_index])

    def _update_link_indices(self):
        rx_indices = []
        tx_indices = []
        for rx_index in range(self.sensor_count):
            for tx_index in range(self.sensor_count):
                if rx_index == tx_index:
                    continue
                rx_indices.append(rx_index)
                tx_indices.append(tx_index)
        self._link_rx_indices = np.asarray(rx_indices, dtype=np.int32)
        self._link_tx_indices = np.asarray(tx_indices, dtype=np.int32)
        self._link_index_by_rx_tx = np.full((self.sensor_count, self.sensor_count), -1, dtype=np.int32)
        for link_index, (rx_index, tx_index) in enumerate(zip(self._link_rx_indices, self._link_tx_indices)):
            self._link_index_by_rx_tx[rx_index, tx_index] = link_index

    def _get_schedule_ms(self, name: str) -> float:
        value_ms = self.appconfig.get(f"{name}_ms")
        if value_ms is not None:
            return float(value_ms)

        legacy_value_us = self.appconfig.get(f"{name}_us")
        if legacy_value_us is not None:
            return float(legacy_value_us) / 1000.0

        return float(self.DEFAULT_CONFIG[f"{name}_ms"])

    def _interpolate_axis_range(self, previous, new):
        if previous is None or not np.isfinite(previous):
            return new
        return previous * 0.92 + new * 0.08

    def _subcarrier_range(self, preamble_format: str):
        if preamble_format not in self._subcarrier_range_cache:
            self._subcarrier_range_cache[preamble_format] = espargos.csi.get_csi_format_subcarrier_indices(preamble_format)
        return self._subcarrier_range_cache[preamble_format]

    @PyQt6.QtCore.pyqtSlot()
    def pollCSI(self):
        if hasattr(self, "pool"):
            self.pool.run()

    def _deserialize_cluster_csi(self, clustered_csi):
        preamble_format = self.genericconfig.get("preamble_format")
        calibration = self.pool.get_calibration()
        if calibration is None:
            return None
        if preamble_format == "lltf":
            if not clustered_csi.has_lltf():
                return None
            csi = calibration.apply_lltf(clustered_csi.deserialize_csi_lltf())
        elif preamble_format == "ht40":
            if not clustered_csi.has_ht40ltf():
                return None
            csi = calibration.apply_ht40(clustered_csi.deserialize_csi_ht40ltf())
            espargos.util.interpolate_ht40ltf_gap(csi)
        elif preamble_format == "he20":
            if not clustered_csi.has_he20ltf():
                return None
            csi = calibration.apply_he20(clustered_csi.deserialize_csi_he20ltf())
            espargos.util.interpolate_he20ltf_gaps(csi)
        else:
            if not clustered_csi.has_ht20ltf():
                return None
            csi = calibration.apply_ht20(clustered_csi.deserialize_csi_ht20ltf())
            espargos.util.interpolate_ht20ltf_gap(csi)
        return csi

    def onCSI(self, clustered_csi: espargos.CSICluster):
        calibration = self.pool.get_calibration()
        if calibration is None:
            return
        tx_index = clustered_csi.get_radar_tx_index()
        if tx_index < 0 or tx_index >= self.sensor_count:
            return

        csi = self._deserialize_cluster_csi(clustered_csi)
        if csi is None:
            return

        completion = np.array(clustered_csi.get_completion().reshape(-1), copy=True)
        if tx_index < completion.size:
            completion[tx_index] = False

        subcarrier_frequencies = self._subcarrier_frequencies_for_format(self.genericconfig.get("preamble_format"))
        corrected = espargos.radar.correct_radar_csi_tx_timestamps(
            csi[np.newaxis, ...],
            np.asarray([clustered_csi.get_radar_tx_info().get_hardware_tx_timestamp_ns() / 1e9], dtype=np.float64),
            np.asarray([tx_index], dtype=np.int32),
            subcarrier_frequencies,
            calibration,
            tx_timestamp_offset_s=float(self.appconfig.get("tx_timestamp_offset_ns")) * 1e-9,
        )[0].reshape(self.sensor_count, -1)

        finite_links = completion & np.all(np.isfinite(corrected), axis=1)
        if not np.any(finite_links):
            return

        power_values = 20.0 * np.log10(np.abs(corrected[finite_links]) + 1e-5)
        finite_power = power_values[np.isfinite(power_values)]
        if finite_power.size == 0:
            return

        self.stable_power_minimum = self._interpolate_axis_range(self.stable_power_minimum, float(np.min(finite_power) - 3.0))
        self.stable_power_maximum = self._interpolate_axis_range(self.stable_power_maximum, float(np.max(finite_power) + 3.0))
        for rx_index in np.flatnonzero(finite_links):
            link_index = int(self._link_index_by_rx_tx[rx_index, tx_index])
            if link_index < 0:
                continue

            self._latest_link_csi[link_index] = np.array(corrected[rx_index], copy=True)
            self._dirty_link_indices.add(link_index)

    @PyQt6.QtCore.pyqtSlot(list, list, PyQt6.QtCharts.QValueAxis)
    def updateCSI(self, powerSeries, phaseSeries, axis):
        if not self._dirty_link_indices:
            if self.stable_power_minimum is not None and self.stable_power_maximum is not None:
                axis.setMin(self.stable_power_minimum)
                axis.setMax(self.stable_power_maximum)
            return

        dirty_link_indices = sorted(self._dirty_link_indices)
        self._dirty_link_indices.clear()

        for link_index in dirty_link_indices:
            if link_index >= len(powerSeries) or link_index >= len(phaseSeries):
                continue
            link_csi = self._latest_link_csi.get(link_index)
            if link_csi is None or not np.all(np.isfinite(link_csi)):
                continue

            subcarrier_range = self._subcarrier_range(self.genericconfig.get("preamble_format"))
            link_power = 20.0 * np.log10(np.abs(link_csi) + 1e-5)
            link_phase = np.angle(link_csi)
            powerSeries[link_index].replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range, link_power)])
            phaseSeries[link_index].replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range, link_phase)])

        axis.setMin(self.stable_power_minimum)
        axis.setMax(self.stable_power_maximum)

    def onAboutToQuit(self):
        try:
            self.disableRadarSchedule()
        except Exception:
            pass
        super().onAboutToQuit()


app = EspargosDemoRadarCSI(sys.argv)
sys.exit(app.exec())
