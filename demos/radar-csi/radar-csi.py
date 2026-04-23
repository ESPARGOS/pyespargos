#!/usr/bin/env python

import argparse
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, SingleCSIFormatMixin

import espargos
import espargos.constants
import espargos.util
import numpy as np
import PyQt6.QtCharts
import PyQt6.QtCore


class EspargosDemoRadarCSI(BacklogMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()
    radarConfigChanged = PyQt6.QtCore.pyqtSignal()
    sensorCountChanged = PyQt6.QtCore.pyqtSignal()
    residualDelayTableChanged = PyQt6.QtCore.pyqtSignal()
    txOffsetTableChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "period_us": 80000,
        "start_us": 10000,
        "slot_us": 10000,
        "tx_power": 34,
        "tx_phymode": 2,
        "tx_rate": 11,
        "rfswitch_state": 2,
        "tx_timestamp_offset_ns": 1063,
        "tx_correction_sign": 1,
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
        self.initial_config["backlog"]["fields"] = {
            "lltf": True,
            "ht20": False,
            "ht40": False,
            "radar_tx_timestamp": True,
            "radar_tx_index": True,
        }

        self.stable_power_minimum = None
        self.stable_power_maximum = None
        self.sensor_count = len(self.get_initial_config("pool", "hosts", default=[])) * espargos.constants.ANTENNAS_PER_BOARD or espargos.constants.ANTENNAS_PER_BOARD
        self._link_rx_indices = np.asarray([], dtype=np.int32)
        self._link_tx_indices = np.asarray([], dtype=np.int32)
        self._subcarrier_range_cache = {}
        self._residual_delay_ns = np.full((self.sensor_count, self.sensor_count), np.nan, dtype=np.float64)
        self._residual_delay_table_text = "No residual delay estimates yet"
        self._tx_timestamp_offsets_ns = np.zeros(self.sensor_count, dtype=np.float64)
        self._tx_offset_table_text = self._format_tx_offset_table()
        self._update_link_indices()

        self.initComplete.connect(self.applyRadarSchedule)
        self.initialize_pool(
            backlog_cb_predicate=lambda cluster: cluster.is_radar() and cluster.has_radar_tx_report() and np.any(cluster.get_completion()),
            calibrate=not self.args.no_calib,
        )
        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "radar-csi-ui.qml")

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)
        self.sensor_count = int(np.prod(self.pool.get_shape()))
        self._update_link_indices()
        self._reset_residual_delay_table()
        self._tx_timestamp_offsets_ns = np.zeros(self.sensor_count, dtype=np.float64)
        self._tx_offset_table_text = self._format_tx_offset_table()
        self.sensorCountChanged.emit()
        self.residualDelayTableChanged.emit()
        self.txOffsetTableChanged.emit()

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

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=residualDelayTableChanged)
    def residualDelayTableText(self):
        return self._residual_delay_table_text

    @PyQt6.QtCore.pyqtProperty(str, constant=False, notify=txOffsetTableChanged)
    def txOffsetTableText(self):
        return self._tx_offset_table_text

    @PyQt6.QtCore.pyqtSlot()
    def estimatePerTxOffsets(self):
        finite_counts = np.sum(np.isfinite(self._residual_delay_ns), axis=0)
        medians = np.full(self.sensor_count, np.nan, dtype=np.float64)
        for tx_index in range(self.sensor_count):
            column = self._residual_delay_ns[:, tx_index]
            finite = column[np.isfinite(column)]
            if finite.size:
                medians[tx_index] = float(np.median(finite))

        valid = np.isfinite(medians) & (finite_counts > 0)
        self._tx_timestamp_offsets_ns[valid] += medians[valid]
        self._tx_offset_table_text = self._format_tx_offset_table()
        self.txOffsetTableChanged.emit()

    @PyQt6.QtCore.pyqtSlot()
    def resetPerTxOffsets(self):
        self._tx_timestamp_offsets_ns = np.zeros(self.sensor_count, dtype=np.float64)
        self._tx_offset_table_text = self._format_tx_offset_table()
        self.txOffsetTableChanged.emit()

    @PyQt6.QtCore.pyqtSlot()
    def applyRadarSchedule(self):
        if not hasattr(self, "pool") or self.pool.get_calibration() is None:
            return

        calibration = self.pool.get_calibration()
        active_by_antid = [True] * espargos.constants.ANTENNAS_PER_BOARD
        requested_start_s = float(self.appconfig.get("start_us")) / 1e6
        min_safe_start_s = max(0.0, -float(np.nanmin(calibration.sensor_clock_offsets))) + 1e-6
        effective_start_s = max(requested_start_s, min_safe_start_s)
        slot_s = float(self.appconfig.get("slot_us")) / 1e6
        t0_by_antid = effective_start_s + np.arange(espargos.constants.ANTENNAS_PER_BOARD, dtype=np.float64) * slot_s
        period_by_antid = np.full(espargos.constants.ANTENNAS_PER_BOARD, float(self.appconfig.get("period_us")) / 1e6, dtype=np.float64)
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
        self.radarConfigChanged.emit()

    @PyQt6.QtCore.pyqtSlot()
    def disableRadarSchedule(self):
        if hasattr(self, "pool"):
            self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})
            self.radarConfigChanged.emit()

    def _subcarrier_count_for_format(self, preamble_format: str) -> int:
        if preamble_format == "lltf":
            return espargos.csi.LEGACY_COEFFICIENTS_PER_CHANNEL
        if preamble_format == "ht40":
            return 2 * espargos.csi.HT_COEFFICIENTS_PER_CHANNEL + espargos.csi.HT40_GAP_SUBCARRIERS
        return espargos.csi.HT_COEFFICIENTS_PER_CHANNEL

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

    def _reset_residual_delay_table(self):
        self._residual_delay_ns = np.full((self.sensor_count, self.sensor_count), np.nan, dtype=np.float64)
        self._residual_delay_table_text = "No residual delay estimates yet"

    def _interpolate_axis_range(self, previous, new):
        if previous is None or not np.isfinite(previous):
            return new
        return previous * 0.92 + new * 0.08

    def _latest_link_csi(self):
        if not hasattr(self, "backlog") or not self.backlog.nonempty():
            return None

        csi_key = self.genericconfig.get("preamble_format")
        try:
            csi_backlog, tx_timestamps_s, tx_indices = self.backlog.get_multiple([csi_key, "radar_tx_timestamp", "radar_tx_index"])
        except ValueError:
            return None

        if csi_backlog.shape[0] == 0:
            return None

        valid_packets = np.flatnonzero(np.isfinite(tx_timestamps_s) & (tx_indices >= 0) & (tx_indices < self.sensor_count))
        if valid_packets.size == 0:
            return None

        # The plot only needs the freshest packet for each transmitter. Walking
        # backwards avoids correcting the whole backlog every QML timer tick.
        selected_packets = []
        seen_tx_indices = set()
        for packet_index in valid_packets[::-1]:
            tx_index = int(tx_indices[packet_index])
            if tx_index in seen_tx_indices:
                continue
            selected_packets.append(packet_index)
            seen_tx_indices.add(tx_index)
            if len(seen_tx_indices) >= self.sensor_count:
                break

        if len(selected_packets) == 0:
            return None

        selected_packets = np.asarray(selected_packets[::-1], dtype=np.int64)
        csi_backlog = csi_backlog[selected_packets]
        tx_timestamps_s = tx_timestamps_s[selected_packets]
        tx_indices = tx_indices[selected_packets]

        if csi_key == "ht40":
            espargos.util.interpolate_ht40ltf_gap(csi_backlog)
        elif csi_key == "ht20":
            espargos.util.interpolate_ht20ltf_gap(csi_backlog)

        calibration = self.pool.get_calibration()
        if calibration is None:
            return None

        subcarrier_frequencies = self._subcarrier_frequencies_for_format(csi_key)
        corrected = espargos.radar.correct_radar_csi_tx_timestamps(
            csi_backlog,
            tx_timestamps_s,
            tx_indices,
            subcarrier_frequencies,
            calibration,
            tx_timestamp_offset_s=float(self.appconfig.get("tx_timestamp_offset_ns")) * 1e-9,
            tx_timestamp_offsets_s=self._tx_timestamp_offsets_ns * 1e-9,
            correction_sign=float(self.appconfig.get("tx_correction_sign")),
        )

        latest = np.full((self.sensor_count, self.sensor_count, corrected.shape[-1]), np.nan + 1.0j * np.nan, dtype=np.complex64)
        corrected_flat = corrected.reshape((corrected.shape[0], self.sensor_count, corrected.shape[-1]))
        for packet_index in range(corrected_flat.shape[0]):
            tx_index = int(tx_indices[packet_index])
            latest[:, tx_index, :] = corrected_flat[packet_index]
            latest[tx_index, tx_index, :] = np.nan + 1.0j * np.nan

        if not np.isfinite(latest).any():
            return None
        return latest

    def _subcarrier_range(self, subcarrier_count: int):
        if subcarrier_count not in self._subcarrier_range_cache:
            self._subcarrier_range_cache[subcarrier_count] = np.arange(-subcarrier_count // 2, subcarrier_count // 2)
        return self._subcarrier_range_cache[subcarrier_count]

    def _update_residual_delay_table(self, latest: np.ndarray):
        csi_key = self.genericconfig.get("preamble_format")
        frequencies = self._subcarrier_frequencies_for_format(csi_key)
        if frequencies.shape[0] != latest.shape[-1]:
            return

        ambiguity_period_ns = 1e9 / espargos.constants.WIFI_SUBCARRIER_SPACING
        residuals = np.full((self.sensor_count, self.sensor_count), np.nan, dtype=np.float64)
        for rx_index in range(self.sensor_count):
            for tx_index in range(self.sensor_count):
                if rx_index == tx_index:
                    continue
                link_csi = latest[rx_index, tx_index]
                if not np.isfinite(link_csi).all():
                    continue

                slope, _intercept = np.polyfit(frequencies, np.unwrap(np.angle(link_csi)), 1)
                delay_ns = -slope / (2.0 * np.pi) * 1e9
                residuals[rx_index, tx_index] = ((delay_ns + ambiguity_period_ns / 2.0) % ambiguity_period_ns) - ambiguity_period_ns / 2.0

        self._residual_delay_ns = residuals
        self._residual_delay_table_text = self._format_residual_delay_table()
        self.residualDelayTableChanged.emit()

    def _format_residual_delay_table(self) -> str:
        cell_width = 9
        lines = [
            "residual phase-slope delay [ns]",
            " " * 6 + "".join(f"TX{tx_index:02d}".rjust(cell_width) for tx_index in range(self.sensor_count)),
        ]
        for rx_index in range(self.sensor_count):
            cells = []
            for tx_index in range(self.sensor_count):
                value = self._residual_delay_ns[rx_index, tx_index]
                if not np.isfinite(value):
                    cells.append(".".rjust(cell_width))
                else:
                    cells.append(f"{value:+.1f}".rjust(cell_width))
            lines.append(f"RX{rx_index:02d}  " + "".join(cells))
        return "\n".join(lines)

    def _format_tx_offset_table(self) -> str:
        cell_width = 10
        lines = [
            "per-TX T1 offset [ns]",
            " ".join(f"TX{tx_index:02d}".rjust(cell_width) for tx_index in range(self.sensor_count)),
            " ".join(f"{value:+.1f}".rjust(cell_width) for value in self._tx_timestamp_offsets_ns),
        ]
        return "\n".join(lines)

    @PyQt6.QtCore.pyqtSlot(list, list, PyQt6.QtCharts.QValueAxis)
    def updateCSI(self, powerSeries, phaseSeries, axis):
        latest = self._latest_link_csi()
        if latest is None:
            return

        subcarrier_count = latest.shape[-1]
        subcarrier_range = self._subcarrier_range(subcarrier_count)
        power_values = 20.0 * np.log10(np.abs(latest) + 1e-5)
        finite_power = power_values[np.isfinite(power_values)]
        if finite_power.size == 0:
            return

        self.stable_power_minimum = self._interpolate_axis_range(self.stable_power_minimum, float(np.min(finite_power) - 3.0))
        self.stable_power_maximum = self._interpolate_axis_range(self.stable_power_maximum, float(np.max(finite_power) + 3.0))
        self._update_residual_delay_table(latest)

        link_count = min(len(powerSeries), len(self._link_rx_indices))
        for link_index in range(link_count):
            rx_index = self._link_rx_indices[link_index]
            tx_index = self._link_tx_indices[link_index]
            link_csi = latest[rx_index, tx_index]
            if not np.isfinite(link_csi).all():
                continue

            link_power = power_values[rx_index, tx_index]
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
