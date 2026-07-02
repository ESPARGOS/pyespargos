#!/usr/bin/env python

import argparse
import pathlib
import sys
import threading

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, RadarControlMixin, RADAR_CONFIG_DEFAULTS

import espargos
import espargos.constants
import numpy as np
import PyQt6.QtCharts
import PyQt6.QtCore


class EspargosDemoRadarCSI(RadarControlMixin, ESPARGOSApplication):
    sensorCountChanged = PyQt6.QtCore.pyqtSignal()
    subcarrierCountChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        **RADAR_CONFIG_DEFAULTS,
        # Multi-antenna TDM: every sensor transmits in turn. With more than one active TX
        # antenna the mixin keeps AGC enabled (no single-TX gain freezing), matching the
        # behavior radar-csi always had.
        "tx_antenna": "all",
        # radar-csi transmits from all antennas, so its per-antenna interval can be a bit
        # longer than the shared default.
        "period_ms": 16.0,
    }

    def __init__(self, argv):
        parser = argparse.ArgumentParser(
            description="ESPARGOS Demo: Show timestamp-corrected radar CSI between TX/RX sensor pairs",
            add_help=False,
        )
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(argv, argparse_parent=parser)

        self.stable_power_minimum = None
        self.stable_power_maximum = None
        self.sensor_count = len(self.get_initial_config("pool", "hosts", default=[])) * espargos.constants.ANTENNAS_PER_BOARD or espargos.constants.ANTENNAS_PER_BOARD
        self._link_rx_indices = np.asarray([], dtype=np.int32)
        self._link_tx_indices = np.asarray([], dtype=np.int32)
        self._link_index_by_rx_tx = np.full((self.sensor_count, self.sensor_count), -1, dtype=np.int32)
        # Current RX data format and matching subcarrier index axis (updated adaptively as CSI arrives)
        self.rx_format = "lltf"
        self._subcarrier_range = espargos.csi.get_csi_format_subcarrier_indices(self.rx_format)
        self._latest_link_csi = {}
        self._dirty_link_indices = set()
        # Guards the CSI state shared between the pool worker thread (onCSI) and the GUI thread (updateCSI)
        self._csi_lock = threading.Lock()
        self._update_link_indices()

        self.init_radar_control()

        self.initialize_pool(
            calibrate=not self.args.no_calib,
        )
        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "radar-csi-ui.qml")

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)
        self.sensor_count = int(np.prod(self.pool.get_shape()))
        self._update_link_indices()
        self.sensorCountChanged.emit()
        # Apply the RX acquire config for the current preamble-format selection (default "auto"
        # accepts every LTF format, so any radar TX format is received and auto-detected).
        self.apply_rx_acquire_config()
        self.pool.add_csi_callback(
            self.onCSI,
            cb_predicate=espargos.radar.radar_completion_predicate("any"),
        )
        # Drive the pool on a background thread. pool.run() blocks up to 0.5 s when no CSI is
        # available, so running it on the GUI thread (as a QTimer) would freeze the UI whenever
        # no packets arrive. onCSI only mutates plain Python state (guarded by _csi_lock); the QML
        # chart series are updated separately on the GUI thread by updateCSI.
        self._pool_thread = threading.Thread(target=self._run_pool_loop, daemon=True)
        self._pool_thread.start()

    def _run_pool_loop(self):
        while True:
            self.pool.run()

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=sensorCountChanged)
    def sensorCount(self):
        return self.sensor_count

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=sensorCountChanged)
    def linkCount(self):
        return self.sensor_count * max(0, self.sensor_count - 1)

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=subcarrierCountChanged)
    def subcarrierCount(self):
        return espargos.csi.get_csi_format_subcarrier_count(self.rx_format)

    @PyQt6.QtCore.pyqtSlot(int, result=str)
    def linkName(self, link_index: int):
        rx_index, tx_index = self._link_indices(link_index)
        return f"TX{tx_index:02d} -> RX{rx_index:02d}"

    def _set_rx_format(self, resolved_format: str):
        """Adapt the display to a changed RX format: new subcarrier axis, drop stale link CSI."""
        self.rx_format = resolved_format
        self._subcarrier_range = espargos.csi.get_csi_format_subcarrier_indices(resolved_format)
        self._latest_link_csi = {}
        self._dirty_link_indices = set()
        self.stable_power_minimum = None
        self.stable_power_maximum = None
        self.subcarrierCountChanged.emit()

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

    def _interpolate_axis_range(self, previous, new):
        if previous is None or not np.isfinite(previous):
            return new
        return previous * 0.92 + new * 0.08

    @PyQt6.QtCore.pyqtSlot()
    def clearCSICurves(self):
        """Drop all stored CSI so the curves reset (they repopulate as new CSI arrives)."""
        with self._csi_lock:
            self._latest_link_csi = {}
            self._dirty_link_indices = set()
            self.stable_power_minimum = None
            self.stable_power_maximum = None

    def onCSI(self, clustered_csi: espargos.CSICluster):
        calibration = self.pool.get_calibration()
        if calibration is None:
            return
        tx_index = clustered_csi.get_radar_tx_index()
        if tx_index < 0 or tx_index >= self.sensor_count:
            return

        resolved_format, csi = espargos.radar.deserialize_rx_csi(clustered_csi, calibration, self.genericconfig.get("preamble_format"))
        if csi is None:
            return
        if resolved_format != self.rx_format:
            with self._csi_lock:
                self._set_rx_format(resolved_format)

        completion = np.array(clustered_csi.get_completion().reshape(-1), copy=True)
        if tx_index < completion.size:
            completion[tx_index] = False

        subcarrier_frequencies = espargos.radar.rx_subcarrier_frequencies(calibration, resolved_format)
        corrected = espargos.radar.correct_radar_csi_tx_timestamps(
            csi[np.newaxis, ...],
            np.asarray([clustered_csi.get_radar_tx_info().get_hardware_tx_timestamp_ns() / 1e9], dtype=np.float64),
            np.asarray([tx_index], dtype=np.int32),
            subcarrier_frequencies,
            calibration.sensor_clock_offsets,
            tx_timestamp_offset_s=float(self.appconfig.get("tx_timestamp_offset_ns")) * 1e-9,
        )[0].reshape(self.sensor_count, -1)

        finite_links = completion & np.all(np.isfinite(corrected), axis=1)
        if not np.any(finite_links):
            return

        power_values = 20.0 * np.log10(np.abs(corrected[finite_links]) + 1e-5)
        finite_power = power_values[np.isfinite(power_values)]
        if finite_power.size == 0:
            return

        with self._csi_lock:
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
        # Snapshot the shared state under the lock (written by the pool thread in onCSI), then do the
        # QML series updates outside the lock so the worker thread is not blocked while we render.
        with self._csi_lock:
            power_minimum = self.stable_power_minimum
            power_maximum = self.stable_power_maximum
            subcarrier_range = self._subcarrier_range
            dirty_link_indices = sorted(self._dirty_link_indices)
            self._dirty_link_indices.clear()
            snapshot = {link_index: self._latest_link_csi.get(link_index) for link_index in dirty_link_indices}

        for link_index in dirty_link_indices:
            if link_index >= len(powerSeries) or link_index >= len(phaseSeries):
                continue
            link_csi = snapshot.get(link_index)
            if link_csi is None or not np.all(np.isfinite(link_csi)):
                continue

            link_power = 20.0 * np.log10(np.abs(link_csi) + 1e-5)
            link_phase = np.angle(link_csi)
            powerSeries[link_index].replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range, link_power)])
            phaseSeries[link_index].replace([PyQt6.QtCore.QPointF(s, p) for s, p in zip(subcarrier_range, link_phase)])

        if power_minimum is not None and power_maximum is not None:
            axis.setMin(power_minimum)
            axis.setMax(power_maximum)

    def onAboutToQuit(self):
        try:
            self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})
        except Exception:
            pass
        super().onAboutToQuit()


app = EspargosDemoRadarCSI(sys.argv)
sys.exit(app.exec())
