#!/usr/bin/env python3
"""MIMO radar with two OFDM transmitters and a configurable receive array."""

import os
import copy
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from collections import deque
import pathlib
import queue
import signal
import sys
import threading
import time

# Avoid BLAS threads competing with packet reception.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np
from PyQt6 import QtCore, QtGui, QtQuick, QtQml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from demos.common.application import ESPARGOSApplication
from espargos import Pool
from espargos.calibration import CalibrationState
from mimo_processing import MIMOProcessor, pilots, C, RATE, SAMPLES, INTERVAL, DOPPLER_WINDOWS, DOPPLER_OVERSAMPLING, SAMPLE_RATES, Sampling, render_maps


class RadarImages(QtQuick.QQuickImageProvider):
    """Pack radar power into textures for the heatmap shader."""

    def __init__(self):
        super().__init__(QtQml.QQmlImageProviderBase.ImageType.Image)
        self.lock = threading.Lock()
        self.images = {}
        self.maximum_db = 30
        self.revision = 0

    def update(self, maps_db):
        images = {}
        maximum_db = -32.0
        for name, values in zip(("doppler", "azimuth"), maps_db):
            # Red/green hold 16-bit power.
            values = np.nan_to_num(values, nan=-32, neginf=-32, posinf=96)
            if name == "doppler":
                maximum_db = float(np.clip(values.max(), -32, 96))
            else:
                # Repeat the first bin to interpolate across the FFT wraparound.
                values = np.concatenate((values, values[:, :1]), axis=1)

            packed = np.rint(np.clip((values + 32) / 128, 0, 1) * 65535).astype(np.uint16)
            rgba = np.zeros((*packed.shape, 4), np.uint8)
            rgba[..., 0] = packed >> 8
            rgba[..., 1] = packed & 255
            rgba[..., 3] = 255
            h, w = packed.shape
            images[name] = QtGui.QImage(rgba.data, w, h, w * 4, QtGui.QImage.Format.Format_RGBA8888).copy()

        with self.lock:
            self.images = images
            self.maximum_db = maximum_db
            self.revision += 1

    def clear(self):
        with self.lock:
            self.images.clear()
            self.maximum_db = 24
            self.revision += 1

    def requestImage(self, name, requestedSize):
        with self.lock:
            image = self.images.get(name.split("/")[0])
            if image is None:
                image = QtGui.QImage(1, 1, QtGui.QImage.Format.Format_RGBA8888)
                image.fill(QtGui.QColor(0, 0, 0, 255))
            return image, image.size()


class MIMORadar(ESPARGOSApplication):
    stateChanged = QtCore.pyqtSignal()
    frameChanged = QtCore.pyqtSignal()
    workerState = QtCore.pyqtSignal(str, bool, bool)
    workerError = QtCore.pyqtSignal(str)

    def _add_config_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("-s", "--single-array", help="IP or hostname of one ESPARGOS array")
        source.add_argument("-c", "--config", help="Combined-array YAML configuration")

    def _add_argparse_arguments(self, parser):
        parser.add_argument("--tx-calibration", choices=("monitor", "reciprocity"), help="Default: monitor for one board, reciprocity for combined arrays")
        parser.add_argument("--frequency", type=float, default=2385, help="Carrier frequency in MHz (default: 2385)")
        parser.add_argument("--amplitude", type=float, default=0.4, help="Digital TX amplitude (default: 0.4)")
        parser.add_argument("--integration-time", type=float, default=DOPPLER_WINDOWS * INTERVAL * SAMPLES / RATE, help="Observation window in seconds, 0.1–2 (default: 0.41). Shorter windows follow rapid motion better.")
        parser.add_argument("--sample-rate", type=float, choices=[rate / 1e6 for rate in SAMPLE_RATES], default=80, help="RX sample rate in MSa/s (default: 80)")
        parser.add_argument("--rx-gain", type=int, default=25, help="Receiver gain index (default: 25)")

    def __init__(self, argv):
        os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Material")
        super().__init__(argv)
        if os.environ.get("QT_QUICK_BACKEND") == "software":
            raise RuntimeError("The radar plots require Qt shaders. Unset QT_QUICK_BACKEND=software before starting the demo.")
        if not 0 < self.args.amplitude <= 1 or not 0 <= self.args.rx_gain <= 76:
            raise ValueError("Use TX amplitude >0 and ≤1, and RX gain 0–76")

        if not 0.1 <= self.args.integration_time <= 2:
            raise ValueError("Use an integration time between 0.1 and 2 seconds")
        self._sampling = Sampling(self.args.sample_rate * 1e6)

        self._status = "Connecting…"
        self._running = False
        self._busy = True
        self._revision = 0
        self._maximum_db = 30.0
        self._beamspace_period = 2.0
        self._tx_feed = self._rx_feed = 1

        self.commands = queue.Queue()
        self.stopping = threading.Event()

        self.provider = RadarImages()
        self.engine.addImageProvider("radar", self.provider)
        self.workerState.connect(self._state)
        self.workerError.connect(self._error)
        self.initialize_qml(pathlib.Path(__file__).with_name("Main.qml"))
        if not self.engine.rootObjects():
            raise RuntimeError("Could not load radar interface")

        self.worker = threading.Thread(target=self._work, name="mimo-radar")
        self.worker.start()

        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.timeout.connect(self._plot)
        self.refresh_timer.start(33)

    @QtCore.pyqtSlot(str, bool, bool)
    def _state(self, status, running, busy):
        self._status, self._running, self._busy = status, running, busy
        self.ready = not busy
        self.initComplete.emit()
        self.stateChanged.emit()

    @QtCore.pyqtSlot(str)
    def _error(self, message):
        self._state(message, False, False)
        self.engine.rootObjects()[0].showError("Radar stopped", message)

    @QtCore.pyqtSlot()
    def _plot(self):
        with self.provider.lock:
            if self._revision == self.provider.revision:
                return
            self._revision = self.provider.revision
            self._maximum_db = self.provider.maximum_db
        self.frameChanged.emit()

    @QtCore.pyqtProperty(str, notify=stateChanged)
    def status(self):
        return self._status

    @QtCore.pyqtProperty(bool, notify=stateChanged)
    def transmitting(self):
        return self._running

    @QtCore.pyqtProperty(bool, notify=stateChanged)
    def busy(self):
        return self._busy

    @QtCore.pyqtProperty(int, notify=frameChanged)
    def revision(self):
        return self._revision

    @QtCore.pyqtProperty(float, notify=frameChanged)
    def maximumDb(self):
        return self._maximum_db

    @QtCore.pyqtProperty(float, notify=stateChanged)
    def beamspacePeriod(self):
        return self._beamspace_period

    @property
    def _history(self):
        return self._sampling.history(self.args.integration_time)

    @QtCore.pyqtProperty("QStringList", constant=True)
    def sampleRates(self):
        return [f"{rate / 1e6:g} MSa/s" for rate in SAMPLE_RATES]

    @QtCore.pyqtProperty(int, notify=stateChanged)
    def sampleRateIndex(self):
        return SAMPLE_RATES.index(self._sampling.rate)

    @QtCore.pyqtProperty(float, notify=stateChanged)
    def rangeMaximum(self):
        return self._sampling.range_maximum

    @QtCore.pyqtProperty(float, notify=stateChanged)
    def velocityLimit(self):
        return C / (4 * self.args.frequency * 1e6 * self._sampling.dt)

    @QtCore.pyqtProperty(float, notify=stateChanged)
    def velocityMinimum(self):
        return -self.velocityLimit * (1 - 2 / (self._history * DOPPLER_OVERSAMPLING))

    @QtCore.pyqtProperty(int, notify=stateChanged)
    def txFeed(self):
        return self._tx_feed

    @QtCore.pyqtProperty(int, notify=stateChanged)
    def rxFeed(self):
        return self._rx_feed

    @QtCore.pyqtSlot(str, float)
    def command(self, name, value):
        if self._busy:
            return
        if name == "sample_rate":
            if not value.is_integer() or not 0 <= value < len(SAMPLE_RATES):
                return
            rate = SAMPLE_RATES[int(value)]
            if rate == self._sampling.rate:
                return
            value = Sampling(rate)
        if name in ("tx_feed", "rx_feed") and value not in (0, 1):
            return
        if not self.worker.is_alive():
            if name not in ("calibrate", "sample_rate"):
                self._error("Calibrate RX + TX to restart the radar.")
                return
            self._busy = True
            self.stateChanged.emit()
            self.worker = threading.Thread(target=self._work, args=(value if name == "sample_rate" else None,), name="mimo-radar")
            self.worker.start()
            return
        if name in ("run", "calibrate", "sample_rate", "tx_feed", "rx_feed"):
            self._busy = True
            self.stateChanged.emit()
        self.commands.put((name, value))

    def _open_radar(self):
        self._frequency = self.args.frequency * 1e6
        self._captures = queue.Queue(maxsize=256)
        self._coverage = 0
        self._capturing = False
        self._callback = None
        self._generations = None

        def status(text):
            self.workerState.emit(text, False, True)

        if self.args.config:
            self._pool = Pool.from_config(self.args.config)
        else:
            self._pool = Pool(self.args.single_array)
        self._pool.open()

        # Keep reading packets during setup so they do not build up.
        self._pool.start_processing()
        for board in self._pool.boards:
            authorization = board.control.get_json("get_transmit_authorization")
            if not authorization["authorized"]:
                raise PermissionError(f"Enable transmit authorization on {board.host} before starting this demo.")

        tx = self._pool.tx
        self._processor = MIMOProcessor(tx.positions.reshape(-1, 2), self._frequency, history=self._history, sample_rate=self._sampling.rate)
        antennas = list(self._pool.antennas)
        self._tx_antennas = [antennas[i] for i in self._processor.tx_indices]
        self._rx_antennas = [antennas[i] for i in self._processor.rx_indices]

        # Incoming IQ uses board/row/column order. Select the receiving antennas.
        boards = {board.host: (i, board) for i, board in enumerate(self._pool.boards)}
        rx_positions = []
        for antenna in self._rx_antennas:
            board_index, board = boards[antenna.host]
            row, column = board.revision.antenna_id_to_row_col(antenna.antid)
            rx_positions.append((board_index, row, column))
        self._index = tuple(np.array(rx_positions).T)

        status(f"Configuring two transmitters and {len(self._rx_antennas)} receivers…")
        self._pool.configure(
            mode="iq_rx",
            frequency_hz=self._frequency,
            sample_rate_hz=self._sampling.rate,
            rx_gain=self.args.rx_gain,
            output=("antenna_l", "antenna_r")[self._rx_feed],
        )
        self._pool.iq.apply_config({"filter_bw_mhz": 0})

        self._transmitters = self._pool.select(antennas=self._tx_antennas)
        self._transmitters.configure(
            mode="iq_tx",
            frequency_hz=self._frequency,
            rf_gain={"a": 2, "b": 0},
            bandwidth_mhz=0,
            output=("antenna_l", "antenna_r")[self._tx_feed],
        )
        with tx.update(samples=self._processor.tx_samples):
            for antenna, waveform in zip(self._tx_antennas, pilots(self._sampling.rate)):
                tx.signals.add(waveform=waveform, amplitude=self.args.amplitude, antennas=[antenna])

        tx_method = self.args.tx_calibration
        if tx_method is None:
            tx_method = "reciprocity" if len(self._pool.hosts) > 1 else "monitor"
        status(f"Calibrating RX and TX through Reference ({tx_method})…")
        self._pool.calibrate(tx_method=tx_method)

        status("Measuring TX–RX timing through Reference…")
        self._timing = self._pool.calibrate_tx_rx_delay()

        self._pool.iq.apply_config({"trigger": {"mode": "accumulate", "vector_chunks": 1, "interval_chunks": self._processor.interval_chunks}})
        self._callback = self._pool.iq.add_accumulation_callback(self._capture)
        self._set_running(True)

        status("Collecting the initial background…")
        self._measure_range_response()

    def _use_calibration(self, calibration):
        """Update RX corrections and check that range zero is still valid."""
        rx_calibration = calibration.select(antennas=self._rx_antennas, direction="rx")
        tx_calibration = calibration.select(antennas=self._tx_antennas, direction="tx")
        active = rx_calibration.merge(tx_calibration)

        self._timing = self._timing.for_calibration(active)
        tx_corrections = np.stack([self._timing.response(antenna, self._frequency + self._processor.offsets) for antenna in self._tx_antennas])
        self._timing_correction = np.repeat(tx_corrections, len(self._rx_antennas), axis=0)

        states = {c.state.antenna: c.state for g in rx_calibration.groups for c in g.corrections}
        if set(states) != set(self._rx_antennas):
            raise RuntimeError("RX calibration is no longer valid. Recalibrate the radar.")

        self._calibration = active
        self._epochs = np.array([states[a].epoch for a in self._rx_antennas], np.uint64)
        self._correction = rx_calibration.response([states[a] for a in self._rx_antennas], self._frequency + self._processor.offsets)

    def _measure_range_response(self, timeout=5):
        """Measure the filter rolloff for the range-response correction."""
        if not self._capturing:
            raise RuntimeError("Start transmission before measuring the range response")
        p = self._processor
        self._clear_history()

        captures = {}
        deadline = time.monotonic() + timeout
        try:
            while len(captures) < 32:
                index, channels = self._next_frame(timeout=max(0, deadline - time.monotonic()))
                captures.setdefault(index, channels)
        except queue.Empty as error:
            raise RuntimeError("Timed out measuring the range response") from error

        self._check_transmitters()
        p.set_range_response(np.mean(list(captures.values()), axis=0))

        # Drop the backlog collected while checking transmitter status.
        self._clear_history()
        self.logger.info("TX–RX hardware delays (ns): %s", {str(a): round(d * 1e9, 3) for a, d in self._timing.delays_s.items()})

    def _check_transmitters(self):
        statuses = dict(zip(self._pool.hosts, self._pool.tx.read()))
        for antenna in self._tx_antennas:
            state = CalibrationState.from_tx_status(antenna.host, statuses[antenna.host]["sensors"][antenna.antid])
            if not self._calibration.covers([state]):
                raise RuntimeError("TX timing changed. Recalibrate RX + TX.")

    def _capture(self, cluster):
        if not self._capturing:
            return

        idx = self._index
        counts, hashes = cluster.counts[idx], cluster.coverage_hashes[idx]
        if (
            cluster.vector_chunks != 1
            or cluster.sample_rate_hz != self._processor.sample_rate
            or cluster.center_freq_hz != self._frequency
            or not np.array_equal(cluster.fire_time_ns[idx], self._epochs)
            or not np.array_equal(cluster.config_generation[idx], self._generations)
        ):
            return

        # Only RX sensors contribute samples.
        if np.any(counts <= 0) or not np.all(counts == counts[0]) or not np.all(hashes == hashes[0]) or np.any(cluster.flags[idx] & 3):
            return

        iq = cluster.iq[idx].copy()
        if not np.all(np.isfinite(iq)):
            return

        self._coverage = float(counts[0, 0]) / self._processor.interval_chunks
        try:
            self._captures.put_nowait((cluster.source_chunk_start, iq))
        except queue.Full:
            pass

    def _set_running(self, running):
        self._capturing = False
        if running:
            self._use_calibration(self._pool.refresh_calibration())
            self._transmitters.start_transmission()
            self._check_transmitters()
            self._capture_boundary()
        else:
            self._transmitters.stop_transmission()

        self._clear_history()
        self._capturing = running

    def _clear_history(self):
        for _ in range(self._captures.qsize()):
            self._captures.get_nowait()
        self._processor.reset()

    def _capture_boundary(self):
        # Advance the generation so buffered captures can be rejected.
        self._pool.iq.apply_config({})
        generations = {b.host: b.iq.get_config()["config_generation"] for b in self._pool.boards}
        self._generations = np.array([generations[a.host] for a in self._rx_antennas], np.uint32)
        self._pool.iq.clear_chunks()

    def _set_feed(self, direction, feed):
        if feed not in ("antenna_l", "antenna_r"):
            raise ValueError("Select a left or right antenna feed")

        running = self._capturing
        self._capturing = False
        if direction == "tx":
            self._transmitters.configure(output=feed)
        elif direction == "rx":
            self._pool.iq.apply_config({"rf_switch": 3 if feed == "antenna_l" else 2})
        else:
            raise ValueError("Select TX or RX")
        self._capture_boundary()

        self._clear_history()
        self._capturing = running

    def _next_frame(self, timeout=1):
        index, iq = self._captures.get(timeout=timeout)
        channels = self._processor.channels(iq, self._correction) * self._timing_correction
        self._processor.push(channels, index)
        return index, channels

    def _close_radar(self):
        self._capturing = False
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                if self._callback is not None:
                    pool.iq.remove_accumulation_callback(self._callback)
            finally:
                pool.close()

    def _work(self, sampling=None):
        # Configuration changes belong to this worker, alongside hardware I/O.
        if sampling is not None:
            self._sampling = sampling
        self._pool = None
        plot_worker = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        pending_plot = None
        plot_valid = False

        try:
            # Start the plotting process before live capture begins.
            plot_worker.submit(os.getpid).result()
            self._open_radar()
            p = self._processor
            self._beamspace_period = p.beamspace_period
            last_plot = last_status = 0
            plot_times = []
            last_capture = time.monotonic()
            self.workerState.emit("Building the first Doppler window…", True, False)

            while not self.stopping.is_set():
                while not self.commands.empty():
                    name, value = self.commands.get_nowait()
                    # Let the current calculation finish, but discard its old data.
                    plot_valid = False
                    if name == "run":
                        self._set_running(bool(value))
                    elif name in ("tx_feed", "rx_feed"):
                        self._set_feed(name[:2], ("antenna_l", "antenna_r")[int(value)])
                        if name == "tx_feed":
                            self._tx_feed = int(value)
                        else:
                            self._rx_feed = int(value)
                    elif name in ("calibrate", "sample_rate"):
                        self.provider.clear()
                        running = self._capturing
                        self._close_radar()
                        if name == "sample_rate":
                            self._sampling = value
                        self._open_radar()
                        p = self._processor
                        plot_times.clear()
                        if not running:
                            self._set_running(False)
                    if name in ("run", "tx_feed", "rx_feed", "calibrate", "sample_rate"):
                        last_capture = time.monotonic()
                        self.workerState.emit("Collecting…" if self._capturing else "Transmission paused", self._capturing, False)

                if not self._capturing:
                    self.stopping.wait(0.05)
                    continue

                try:
                    self._next_frame(timeout=0.05)
                    last_capture = time.monotonic()
                except queue.Empty:
                    if time.monotonic() - last_capture > 5:
                        raise RuntimeError("No coherent accumulated IQ for five seconds. Check the array and recalibrate.")
                    continue

                # Process queued captures before drawing the next frame.
                for _ in range(self._captures.qsize()):
                    self._next_frame(timeout=0)

                now = time.monotonic()
                if pending_plot is not None and pending_plot.done():
                    maps_db = pending_plot.result()
                    pending_plot = None
                    if plot_valid and maps_db is not None:
                        self.provider.update(maps_db)
                        plot_times.append(now)
                        plot_times = [t for t in plot_times if now - t < 2]

                if pending_plot is None and now - last_plot >= 0.05 and len(p.frames) == p.history:
                    snapshot = copy.copy(p)
                    snapshot.frames = deque(p.frames)
                    pending_plot = plot_worker.submit(render_maps, snapshot)
                    plot_valid = True
                    last_plot = now

                if now - last_status >= 0.5:
                    fps = (len(plot_times) - 1) / max(plot_times[-1] - plot_times[0], 0.01) if len(plot_times) > 1 else 0
                    self.workerState.emit(f"Input coverage {self._coverage:.1%} · {fps:.0f} plots/s · {p.missing} missing windows", True, False)
                    last_status = now
        except Exception as error:
            self.logger.exception("Radar stopped")
            self.workerError.emit(str(error))
        finally:
            if self._pool is not None:
                try:
                    self._close_radar()
                except Exception as error:
                    self.workerError.emit(f"Could not confirm TX shutdown: {error}")
            plot_worker.shutdown(wait=True, cancel_futures=True)

    def onAboutToQuit(self):
        self.stopping.set()
        if hasattr(self, "worker"):
            self.worker.join()
        super().onAboutToQuit()


if __name__ == "__main__":
    app = MIMORadar(sys.argv)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    sys.exit(app.exec())
