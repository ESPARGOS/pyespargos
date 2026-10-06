#!/usr/bin/env python
"""ESPARGOS demo: live IQ signal analyzer.

Puts the array into IQ sampling mode and renders, per sensor, one of three live
displays, built from INDEX-MATCHED chunk sets (the array-wide time sync makes
equal source_chunk_index the same instant, so rows/traces line up in time
across all sensors):

  - power waterfall: FFT rows on a fixed -90..0 dBFS scale (viridis)
  - relative-phase waterfall vs sensor 0 (twilight), with the fine per-sensor
    time/phase calibration applied when the Apply-calibration toggle is on
  - time-domain I/Q trace (all sensors show the same instant)
  - I/Q constellation (the same synchronized sample window per sensor)
  - amplitude spectrum over absolute RF frequency

Capture control (frequency, sample rate, analog filter bandwidth, RF switch,
gain, triggers), array time sync and phase calibration come from the reusable
espargos interfaces: IQPool assembles the chunk streams into
synchronized clusters and manages the IQ lifecycle, IQController adapts it to
the drawer UI (IQSettings.qml).

With several comma-separated hosts (a coherent multi-board setup, e.g. the
Aperture Kit) a tab bar selects which board's sensors are displayed — every
cluster spans all boards, so the tab switch just changes which slice is
rendered; config, sync (array-wide, REFTX-packet-anchored) and calibration
(global, via the master's reference tone) always apply to the whole array.

Usage: iq-signal-analyzer.py <controller-host>[,<controller-host>,...]
(tests live in the repository tests/ directory)
"""

import argparse
import pathlib
import sys
import threading
import time

# pyespargos repository root
sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import numpy as np
import matplotlib.cm
import matplotlib.colors

import PyQt6.QtCore
import PyQt6.QtCharts

import espargos

from espargos import CHUNK_SAMPLES, DECIM_TO_FS, IQAccumCluster, SENSOR_COUNT
from demos.common.iq_application import ESPARGOSIQApplication
from demos.common.iq_waterfall import WaterfallImageProvider, power_db_row

WATERFALL_HEIGHT = 300
ADC_FULL_SCALE = 512.0
DB_MIN, DB_MAX = -90.0, 0.0

PHASE_MAPPER = matplotlib.cm.ScalarMappable(norm=matplotlib.colors.Normalize(vmin=-np.pi, vmax=np.pi, clip=True), cmap="twilight")


def pool_complex_to_width(z, w):
    """Reduce/expand a complex spectrum to w pixels by coherent averaging."""
    n = len(z)
    if w == n:
        return z
    if w < n:
        edges = (np.arange(w) * n) // w
        return np.add.reduceat(z, edges)
    return z[(np.arange(w) * n) // w]


class EspargosSignalAnalyzer(ESPARGOSIQApplication):
    waterfallFrameReady = PyQt6.QtCore.pyqtSignal()
    displayModeChanged = PyQt6.QtCore.pyqtSignal()
    traceSampleCountChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "display_mode": "power",  # "power", "phase", "time", "constellation", "spectrum"
        "fft_size": 1024,  # 256 / 512 / 1024; also the time-domain trace length (needs contiguous bursts >= fft/256 chunks)
        "complete_only": False,  # render only sets where ALL sensors delivered the chunk
        "apply_calibration": True,  # apply the fine per-sensor calibration to the phase display
    }

    def __init__(self, argv):
        parser = argparse.ArgumentParser(description="ESPARGOS Demo: live IQ signal analyzer", add_help=False)
        super().__init__(argv, argparse_parent=parser)

        self.iqcontrol = None  # IQController QML adapter, created once the pool is up
        self.active_board = 0
        # clusters delivered by the pool's processing worker, drained by the
        # render thread (bounded so a stalled render cannot grow it unboundedly)
        self._pending_lock = threading.Lock()
        self._pending_clusters = []
        self.display_mode = self.get_initial_config("app", "display_mode")
        self.fft_size = int(self.get_initial_config("app", "fft_size"))
        self.complete_only = bool(self.get_initial_config("app", "complete_only"))
        self.display_width = CHUNK_SAMPLES
        self._win = np.hanning(self.fft_size).astype(np.float32)
        self._fft_scale = max(float(np.sum(self._win)) * ADC_FULL_SCALE, 1.0)
        self.block_acc = {}  # block_id -> {antid: {slot: iq}}
        self.block_time = {}  # block_id -> last-update wall time (stale eviction)
        self.rows_rendered = [0] * SENSOR_COUNT
        self.render_running = False
        self._first_sets_logged = False
        self._sample_window_lock = threading.Lock()
        self._sample_windows = [np.zeros(self.fft_size, dtype=np.complex64) for _ in range(SENSOR_COUNT)]
        self._sample_window_present = [False] * SENSOR_COUNT
        self._sample_window_generation = 0
        self._time_trace_consumed = [-1] * SENSOR_COUNT
        self._constellation_consumed = [-1] * SENSOR_COUNT
        self._spectrum_consumed = [-1] * SENSOR_COUNT
        self._spectrum_sample_rate_hz = DECIM_TO_FS[1]
        self._spectrum_center_hz = 2437e6
        self._spectrum_config_read_at = 0.0

        self.providers = [WaterfallImageProvider(CHUNK_SAMPLES, WATERFALL_HEIGHT) for _ in range(SENSOR_COUNT)]
        for antid in range(SENSOR_COUNT):
            self.engine.addImageProvider(f"display{antid}", self.providers[antid])

        self.appConfigChanged.connect(self._on_app_config_changed)

    # ---- configuration ----

    def _on_app_config_changed(self, newcfg):
        if "display_mode" in newcfg:
            self._set_display_mode(str(newcfg["display_mode"]))
        if "fft_size" in newcfg:
            self._set_fft_size(int(newcfg["fft_size"]))
        if "complete_only" in newcfg:
            self.complete_only = bool(newcfg["complete_only"])
        if "apply_calibration" in newcfg:
            if self.iqcontrol is not None:
                self.iqcontrol.set_apply_calibration(bool(newcfg["apply_calibration"]))

    def _set_display_mode(self, mode):
        if mode != self.display_mode:
            self.display_mode = mode
            for p in self.providers:
                p.clear()
            if mode in ("time", "constellation", "spectrum"):
                self._publish_sample_windows({})
            self.waterfallFrameReady.emit()
            self.displayModeChanged.emit()

    def _set_fft_size(self, n):
        if n == self.fft_size or n < CHUNK_SAMPLES or n % CHUNK_SAMPLES != 0:
            return
        self.fft_size = n
        self._win = np.hanning(n).astype(np.float32)
        self._fft_scale = max(float(np.sum(self._win)) * ADC_FULL_SCALE, 1.0)
        self.block_acc = {}
        self.block_time = {}
        for p in self.providers:
            p.clear()
        self._publish_sample_windows({})
        self.waterfallFrameReady.emit()
        self.traceSampleCountChanged.emit()

    @PyQt6.QtCore.pyqtSlot(int)
    def set_display_width(self, w):
        """Follow the window size: waterfall pixel width per sensor."""
        w = max(128, min(2048, (int(w) // 32) * 32))
        if w == self.display_width:
            return
        self.display_width = w
        for p in self.providers:
            p.resize(w)
        self.waterfallFrameReady.emit()

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def sensorCount(self):
        return SENSOR_COUNT

    @PyQt6.QtCore.pyqtProperty(bool, notify=displayModeChanged)
    def timeDomain(self):
        return self.display_mode == "time"

    @PyQt6.QtCore.pyqtProperty(bool, notify=displayModeChanged)
    def constellation(self):
        return self.display_mode == "constellation"

    @PyQt6.QtCore.pyqtProperty(bool, notify=displayModeChanged)
    def spectrum(self):
        return self.display_mode == "spectrum"

    @PyQt6.QtCore.pyqtProperty(int, notify=traceSampleCountChanged)
    def traceSampleCount(self):
        return self.fft_size

    @PyQt6.QtCore.pyqtProperty(float, constant=True)
    def adcFullScale(self):
        return ADC_FULL_SCALE

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def boardCount(self):
        return len(self.pool.boards) if hasattr(self, "pool") else 1

    @PyQt6.QtCore.pyqtProperty("QVariantList", constant=True)
    def boardHosts(self):
        return [b.host for b in self.pool.boards] if hasattr(self, "pool") else []

    @PyQt6.QtCore.pyqtSlot(int)
    def set_active_board(self, index):
        """Tab switch: render this board's waterfalls (only the active board
        is rendered — with 4+ boards rendering everything is too costly; the
        clusters span all boards, so switching just changes the slice)."""
        if index == self.active_board or not hasattr(self, "pool") or not (0 <= index < len(self.pool.boards)):
            return
        self.active_board = index
        self.block_acc = {}
        self.block_time = {}
        for p in self.providers:
            p.clear()
        if self.display_mode in ("time", "constellation", "spectrum"):
            self._publish_sample_windows({})

        self.waterfallFrameReady.emit()

    # ---- rendering ----

    def _placeholder_row(self):
        return np.full((self.display_width, 4), (40, 40, 46, 255), dtype=np.uint8)

    def _display_spectrum(self, x):
        """Windowed, fftshifted FFT; x must be fft_size samples long."""
        return np.fft.fftshift(np.fft.fft(x * self._win))

    def _power_row(self, spec):
        db = 20 * np.log10(np.maximum(np.abs(spec) / self._fft_scale, 1e-12))
        return power_db_row(db, self.display_width, DB_MIN, DB_MAX)

    def _phase_row(self, rel):
        pooled = pool_complex_to_width(rel, self.display_width)
        return (PHASE_MAPPER.to_rgba(np.angle(pooled)[np.newaxis, :]) * 255).astype(np.uint8)[0]

    def start_render(self):
        if self.render_running:
            return
        self.render_running = True
        threading.Thread(target=self._render, daemon=True).start()

    def _publish_sample_windows(self, traces):
        """Publish coherent display windows for the GUI-thread Qt Charts.

        IQ assembly stays in the render worker; chart-series updates happen
        only when QML calls the update slots on the GUI thread.
        """
        with self._sample_window_lock:
            self._sample_window_present = [antid in traces for antid in range(SENSOR_COUNT)]
            self._sample_windows = [np.asarray(traces.get(antid, np.zeros(self.fft_size, dtype=np.complex64))).copy() for antid in range(SENSOR_COUNT)]
            self._sample_window_generation += 1

    @PyQt6.QtCore.pyqtSlot(int, PyQt6.QtCharts.QLineSeries, PyQt6.QtCharts.QLineSeries, result="QVariantMap")
    def updateTimeChart(self, antid, i_series, q_series):
        """Replace one chart's I/Q series and return window RMS/peak dBFS."""
        if not (0 <= antid < SENSOR_COUNT):
            return {}
        with self._sample_window_lock:
            generation = self._sample_window_generation
            if self._time_trace_consumed[antid] == generation:
                return {}
            iq = self._sample_windows[antid].copy()
            self._time_trace_consumed[antid] = generation
        i_series.replace([PyQt6.QtCore.QPointF(x, float(y)) for x, y in enumerate(iq.real)])
        q_series.replace([PyQt6.QtCore.QPointF(x, float(y)) for x, y in enumerate(iq.imag)])
        magnitude = np.abs(iq)
        rms = float(np.sqrt(np.mean(np.square(magnitude), dtype=np.float64)))
        peak = float(np.max(magnitude)) if magnitude.size else 0.0

        def dbfs(value):
            return float(20 * np.log10(value / ADC_FULL_SCALE)) if value > 0 else float("-inf")

        return {"rmsDbfs": dbfs(rms), "peakDbfs": dbfs(peak)}

    @PyQt6.QtCore.pyqtSlot(int, PyQt6.QtCharts.QScatterSeries)
    def updateConstellationChart(self, antid, series):
        """Replace one sensor's constellation from the newest IQ window."""
        if not (0 <= antid < SENSOR_COUNT):
            return
        with self._sample_window_lock:
            generation = self._sample_window_generation
            if self._constellation_consumed[antid] == generation:
                return
            present = self._sample_window_present[antid]
            iq = self._sample_windows[antid].copy()
            self._constellation_consumed[antid] = generation
        if not present:
            series.replace([])
            return
        series.replace([PyQt6.QtCore.QPointF(float(value.real), float(value.imag)) for value in iq])

    @PyQt6.QtCore.pyqtSlot(int, PyQt6.QtCharts.QLineSeries, PyQt6.QtCharts.QValueAxis)
    def updateSpectrumChart(self, antid, series, frequency_axis):
        """Replace one sensor's two-sided, windowed amplitude spectrum."""
        if not (0 <= antid < SENSOR_COUNT):
            return
        with self._sample_window_lock:
            generation = self._sample_window_generation
            if self._spectrum_consumed[antid] == generation:
                return
            present = self._sample_window_present[antid]
            iq = self._sample_windows[antid].copy()
            sample_rate_hz = self._spectrum_sample_rate_hz
            center_hz = self._spectrum_center_hz
            self._spectrum_consumed[antid] = generation
        frequency_axis.setRange((center_hz - sample_rate_hz / 2) / 1e6, (center_hz + sample_rate_hz / 2) / 1e6)
        if not present:
            series.replace([])
            return
        spectrum = self._display_spectrum(iq)
        amplitude_dbfs = 20 * np.log10(np.maximum(np.abs(spectrum) / self._fft_scale, 1e-12))
        frequencies_mhz = (center_hz + np.fft.fftshift(np.fft.fftfreq(len(iq), d=1 / sample_rate_hz))) / 1e6
        series.replace([PyQt6.QtCore.QPointF(float(frequency), float(amplitude)) for frequency, amplitude in zip(frequencies_mhz, amplitude_dbfs)])

    def _refresh_spectrum_config(self):
        """Track RF center/sample rate without blocking the GUI thread."""
        now = time.monotonic()
        if now - self._spectrum_config_read_at < 1.0:
            return
        self._spectrum_config_read_at = now
        try:
            cfg = self.pool.get_config()
            sample_rate_hz = DECIM_TO_FS.get(int(cfg.get("adc_decimation", 1)), DECIM_TO_FS[1])
            receivers = cfg.get("receivers", [])
            center_hz = float(receivers[0].get("rf_freq_hz", 2437e6)) if receivers else 2437e6
            with self._sample_window_lock:
                self._spectrum_sample_rate_hz = sample_rate_hz
                self._spectrum_center_hz = center_hz
        except Exception as e:
            print(f"spectrum config refresh error: {e}", file=sys.stderr)

    def _on_iq_cluster(self, cluster):
        """Pool cluster callback (runs on the pool's processing thread; keep
        it light). Complete clusters arrive immediately, settled partial ones
        after the settle timeout — the block assembler reorders by index."""
        with self._pending_lock:
            self._pending_clusters.append(cluster)
            if len(self._pending_clusters) > 2048:
                del self._pending_clusters[:1024]

    def _drain_sets(self):
        """Drain raw or accumulated clusters into 256-sample display sets.

        An accumulated vector is split back into its ordered vector sections,
        using ``source_chunk_start + lane`` as the display-grid index. This is
        the same mapping as the controller web waterfall and lets the existing
        fft_size/256 block assembler consume either wire format unchanged.
        """
        with self._pending_lock:
            clusters, self._pending_clusters = self._pending_clusters, []
        board = self.active_board
        if not clusters or board >= len(self.pool.boards):
            return []
        revision = self.pool.boards[board].revision
        positions = [revision.antenna_id_to_row_col(antenna_id) for antenna_id in range(SENSOR_COUNT)]
        sets = []
        for cluster in clusters:
            if isinstance(cluster, IQAccumCluster):
                completion = cluster.section_completion
                sections = cluster.iq_sections
                for lane in range(cluster.vector_chunks):
                    per = {antid: sections[board, row, col, lane] for antid, (row, col) in enumerate(positions) if completion[board, row, col, lane]}
                    if per:
                        sets.append((cluster.source_chunk_start + lane, per))
            else:
                completion = cluster.completion
                iq = cluster.iq
                per = {antid: iq[board, row, col] for antid, (row, col) in enumerate(positions) if completion[board, row, col]}
                if per:
                    sets.append((cluster.chunk_index, per))
        sets.sort(key=lambda entry: entry[0])
        return sets

    def _render(self):
        # Include processing time in the 25-Hz frame budget. Sleeping a full
        # interval after rendering made the scroll speed depend on FFT/copy
        # cost. QML refreshes only when this worker publishes a complete frame.
        next_frame = time.monotonic()
        while self.render_running:
            time.sleep(max(0.0, next_frame - time.monotonic()))
            if not self.render_running:
                break
            next_frame = time.monotonic() + 1 / 25
            try:
                sets = self._drain_sets()
                if not sets:
                    continue
                if not self._first_sets_logged:
                    self._first_sets_logged = True
                    self.logger.info("first synchronized IQ chunk sets received, display running")
                mode = self.display_mode
                if mode == "spectrum":
                    self._refresh_spectrum_config()
                # Every display mode goes through the block assembler (B >= 1
                # chunks per display unit): it renders strictly ordered by
                # chunk index and thereby also absorbs the out-of-order
                # delivery of settled partial sets behind complete ones.
                self._render_block_rows(sets, mode)
            except Exception as e:
                print(f"render error: {e}", file=sys.stderr)

    def _render_block_rows(self, sets, mode):
        """Assemble fft_size/256 CONSECUTIVE chunks per sensor into one
        display unit, a waterfall row or one time-domain trace window (blocks
        aligned to the synchronized index grid, so rows and traces line up in
        time across sensors; a sensor that did not deliver a block gets a
        placeholder row).

        Eviction: a block renders once a NEWER block exists (the normal case —
        the session's unwrapped chunk index is monotonic) or once it has not
        been touched for a while (defense against a stale higher key, e.g. a
        post-sync straggler from the old time base, permanently masking the
        `b < max_block` rule — that failure mode rendered ~25% of rows broken
        after the first index wrap before the index was unwrapped)."""
        B = self.fft_size // CHUNK_SAMPLES
        now = time.time()
        for idx, per in sets:
            b = idx // B
            block = self.block_acc.setdefault(b, {})
            for antid, iq in per.items():
                block.setdefault(antid, {})[idx % B] = iq
            self.block_time[b] = now
        max_block = max(self.block_acc.keys())
        ready = sorted(b for b in self.block_acc.keys() if b < max_block or now - self.block_time.get(b, now) > 2.0)
        if mode in ("time", "constellation", "spectrum"):
            self._render_sample_blocks(ready, B)
            return
        rows = [[] for _ in range(SENSOR_COUNT)]
        for b in ready:
            block = self.block_acc.pop(b, None)
            self.block_time.pop(b, None)
            if block is None:
                continue  # block_acc was swapped by a concurrent FFT-size change
            specs = {}
            for antid, slots in block.items():
                if len(slots) == B:
                    specs[antid] = self._display_spectrum(np.concatenate([slots[k] for k in range(B)]))
            if self.complete_only and len(specs) < SENSOR_COUNT:
                continue
            for antid in range(SENSOR_COUNT):
                if mode == "phase":
                    if antid in specs and 0 in specs:
                        rel = specs[antid] * np.conj(specs[0])
                        corr = self.iqcontrol.display_correction(self.active_board, antid, self.fft_size) if self.iqcontrol is not None else None
                        if corr is not None:
                            rel = rel * corr
                        row = self._phase_row(rel)
                    else:
                        row = self._placeholder_row()
                else:
                    row = self._power_row(specs[antid]) if antid in specs else self._placeholder_row()
                rows[antid].append(row)
        # Shift each waterfall once per display frame, not once per FFT row.
        # At FFT=256 this avoids hundreds of full-image copies per second.
        if rows[0]:
            for antid in range(SENSOR_COUNT):
                self.providers[antid].add_rows(np.asarray(rows[antid]))
                self.rows_rendered[antid] += len(rows[antid])
            self.waterfallFrameReady.emit()

    def _render_sample_blocks(self, ready, B):
        """Chart display at fft_size above one chunk: all sensors show the SAME
        newest ready block of fft_size samples (the multi-chunk analogue of
        _render_sample_windows, same complete_only semantics: with it,
        only blocks where ALL sensors delivered every chunk are shown; without
        it, the newest block with at least one full per-sensor trace is shown
        and missing sensors are drawn as an empty grid)."""
        chosen = None
        for b in reversed(ready):
            block = self.block_acc.get(b)
            if block is None:
                continue
            traces = {antid: np.concatenate([slots[k] for k in range(B)]) for antid, slots in block.items() if len(slots) == B}
            if len(traces) >= (SENSOR_COUNT if self.complete_only else 1):
                chosen = traces
                break
        for b in ready:
            self.block_acc.pop(b, None)
            self.block_time.pop(b, None)
        if chosen is None:
            return
        self._publish_sample_windows(chosen)
        for antid in range(SENSOR_COUNT):
            self.rows_rendered[antid] += 1

    # ---- application lifecycle ----

    def _start_iq_gui(self):
        """After the framework pool is up: expose the pool's QML adapter,
        subscribe the display to delivered clusters, and enter IQ mode. Sync
        (anchors from WiFi packets) BEFORE entering IQ."""
        hosts = ", ".join(b.host for b in self.pool.boards)
        self.logger.info(f"IQ capture starting ({len(self.pool.boards)} board(s): {hosts})")
        self.iqcontrol = self.create_iq_controller()
        self.iqcontrol.set_apply_calibration(bool(self.appconfig.get("apply_calibration")))
        self.engine.rootContext().setContextProperty("iqcontrol", self.iqcontrol)
        # the display also wants settled partial sets (missing sensors render
        # as placeholder rows instead of stalling the waterfall)
        self.pool.add_iq_callback(self._on_iq_cluster, include_partial=True)
        self.pool.add_accumulation_callback(self._on_iq_cluster, include_partial=True)

        def _enter():
            try:
                self.logger.info("acquiring array-wide time sync (this anchors all sensors' " "IQ chunk grids to one reference packet; takes a few seconds)...")
                self.pool.sync()  # blocking, hence this worker thread
                self.logger.info("time sync done, switching the array to IQ sampling mode...")
                cfg = self.pool.get_config()
                cfg["mode"] = "iq"
                self.pool.apply_config(cfg)  # sensors fire on the anchored grid
                self.logger.info("IQ mode entered; waiting for the first synchronized chunk sets...")
            except Exception as e:
                self.logger.error(f"could not enter IQ mode: {e}")

        threading.Thread(target=_enter, daemon=True).start()
        self.start_render()

    def onAboutToQuit(self):
        self.render_running = False
        super().onAboutToQuit()  # ESPARGOSIQApplication restores WiFi mode

    # ---- entry point ----

    def exec(self):
        self.initComplete.connect(self._start_iq_gui)
        self.initialize_pool(calibrate=False)
        # iqcontrol is created after pool init; QML needs the name defined from
        # the start (bindings re-evaluate when the real adapter replaces None)
        self.initialize_qml(pathlib.Path(__file__).resolve().parent / "iq-signal-analyzer-ui.qml", context_props={"iqcontrol": None})
        return super().exec()


if __name__ == "__main__":
    app = EspargosSignalAnalyzer(sys.argv)
    sys.exit(app.exec())
