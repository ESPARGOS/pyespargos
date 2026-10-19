"""Qt adapter for TX demos. One operation in flight, latest edits queued by kind."""

import copy
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from espargos.iq_tx import TXSession as Session, waveform, load_iq, default_beam
from pathlib import Path


class IQTxController(QObject):
    """Reusable controller for IQTxDrawer and IQTxWorkspace.

    All hardware calls use a serial worker. Edits remain on the Qt thread and
    are copied before dispatch, so replies never replace newer user input.
    """

    changed = pyqtSignal()
    error = pyqtSignal(str)
    closed = pyqtSignal()
    monitorSamples = pyqtSignal(object)
    _completed = pyqtSignal(object, object, object)

    def __init__(self, layout, parent=None, session_factory=Session, all_tx=False):
        super().__init__(parent)
        self.layout, self.session_factory = layout, session_factory
        self.all_tx = all_tx
        self.session = self.monitor = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="iq-tx-ui")
        self.pending = set()
        self.closing = self.shutdown_started = False
        self.steering_cleared = False
        self.file_previews = {}
        self.starting = False
        self.data = dict(
            connected=False,
            authorized=False,
            busy=False,
            exclusive=False,
            calibrating=False,
            playing=False,
            closing=False,
            monitor=False,
            unlocked=False,
            message="Connecting…",
            hosts=layout.hosts,
            names=layout.names,
            count=1,
            samples=1024,
            beams=[default_beam(len(layout.hosts)) for _ in range(4)],
            modes=[["wifi"] * 8 for _ in layout.hosts],
            sensors=[],
            frequency=2380.0,
            gain_a=0,
            gain_b=0,
            bandwidth=40,
            output="antenna_r",
            route="reference",
            monitor_gain=12,
            monitor_rate=80000000,
            monitor_samples=1024,
            monitor_interval=50000,
            monitor_offset=0,
            monitor_bw=40,
        )
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(45)
        self.timer.timeout.connect(self.flush)
        self.poll = QTimer(self)
        self.poll.setInterval(50)
        self.poll.timeout.connect(self._monitor_frame)
        self.poll.start()
        self._completed.connect(self._done)

    @pyqtProperty("QVariantMap", notify=changed)
    def state(self):
        return self.data

    @pyqtSlot()
    def connectArray(self):
        if self.data["busy"] or self.closing:
            return

        def connect():
            if self.session is None:
                self.session = self.session_factory(self.layout)
            elif not self.data["connected"]:
                self.session.reconnect()
            self.session.authorization = self.session.authorized()
            states = self.session.read()
            if self.all_tx and all(self.session.authorization):
                modes = [["iq_tx"] * 8 for _ in self.layout.hosts]
                if any(s["modes"] != m for s, m in zip(states, modes)):
                    states = self.session.modes(modes)
            return states, self.session.authorization

        self._run(connect, self._connected, exclusive=True)

    def _connected(self, result):
        states, authorized = result
        self.data.update(connected=True, authorized=all(authorized), playing=False, unlocked=False)
        self.starting = False
        self.poll.start()
        self.data["message"] = (
            "Preparing reference calibration…"
            if self.all_tx and all(authorized)
            else "Calibrate the aperture, then start transmission." if all(authorized) else "Enable transmit authorization in each controller’s Settings, then reconnect."
        )
        first = states[0]["sensors"][0]
        self.data.update(
            frequency=first["frequency_hz"] / 1e6,
            gain_a=min(first["rf_gain"]["a"], 2),
            gain_b=0,
            bandwidth=first["bandwidth_mhz"],
            output="antenna_r" if self.all_tx else first["output"],
            samples=first["samples"] or 1024,
            modes=[s["modes"][:] for s in states],
        )
        self._status(states)
        self._queue("monitor")
        if self.all_tx and all(authorized):
            self.data.update(route="reference", calibrating=True)
            self._queue("calibrate", "start", "rf")

    def _status(self, states):
        self.data["sensors"] = [
            dict(name=f"{self.layout.names[i]} / {a}", board=i, antenna=a, phase=state["calibration"]["phase_deg"][a], delay=state["calibration"]["delay_samples"][a])
            for i, state in enumerate(states)
            for a in range(8)
        ]

    def _run(self, fn, callback=None, exclusive=False):
        assert not self.data["busy"]
        self.data.update(busy=True, exclusive=exclusive)
        self.changed.emit()
        future = self.executor.submit(fn)

        def complete(f):
            try:
                result = f.result()
            except Exception as error:
                self._completed.emit(callback, None, error)
            else:
                self._completed.emit(callback, result, None)

        future.add_done_callback(complete)

    def _done(self, callback, result, error):
        self.data.update(busy=False, exclusive=False)
        if not error and callback:
            try:
                callback(result)
            except Exception as callback_error:
                error = callback_error
        if error:
            self.pending.clear()
            self.data["calibrating"] = False
            self.starting = False
            if getattr(error, "tx_stopped", False):
                self.data["playing"] = False
            else:
                self.data.update(connected=False, authorized=False, monitor=False)
                self.steering_cleared = False
            self.data["message"] = str(error)
            self.error.emit(str(error))
            if self.closing:
                self.closing = self.shutdown_started = False
                self.data.update(closing=False, connected=False, authorized=False)
        self.changed.emit()
        if self.closing:
            if self.shutdown_started:
                self.executor.shutdown(wait=False)
                self.closed.emit()
            else:
                self._shutdown()
        elif self.pending:
            QTimer.singleShot(0, self.flush)

    def _queue(self, *kinds):
        if self.data["authorized"] and not self.closing:
            self.pending.update(kinds)
            if not self.timer.isActive():
                self.timer.start()

    @pyqtSlot(str, "QVariant")
    def setSetting(self, key, value):
        if self.closing or self.data["exclusive"]:
            return
        integer = {"count", "samples", "gain_a", "gain_b", "bandwidth", "monitor_gain", "monitor_rate", "monitor_samples", "monitor_interval", "monitor_offset", "monitor_bw"}
        if key not in integer | {"frequency", "output", "route", "unlocked"}:
            return
        if key in integer:
            value = int(value)
        if key == "frequency":
            value = float(value)
        if key in {"gain_a", "gain_b"}:
            maximum = 7 if self.data["unlocked"] else (2 if key == "gain_a" else 0)
            value = min(maximum, max(0, value))
        if key == "count" and not 1 <= value <= 4 or key == "samples" and value not in (256, 512, 1024, 2048, 4096, 8192):
            return
        if isinstance(value, (int, float)) and not np.isfinite(value):
            return
        if self.data[key] == value:
            return
        self.data[key] = value
        if key == "unlocked" and not value:
            self.data["gain_a"] = min(2, self.data["gain_a"])
            self.data["gain_b"] = 0
            for beam in self.data["beams"]:
                beam["amplitude"] = min(0.4, beam["amplitude"])
            self._queue("rf", "waveform")
        elif key.startswith("monitor_"):
            self._queue("monitor")
        elif key in {"count", "samples"}:
            self._queue("waveform")
            if key == "samples":
                self._queue("stop")
                self.data["message"] = "Replay length changed. Calibrate again."
        elif key in {"frequency", "gain_a", "gain_b", "bandwidth", "output"}:
            self._queue("rf")
            if key == "frequency":
                self._queue("stop", "monitor", "waveform")
                self.data["message"] = "Frequency changed. Calibrate again."
        self.changed.emit()

    @pyqtSlot(int, str, "QVariant")
    def setBeam(self, index, key, value):
        if self.closing or self.data["exclusive"] or not 0 <= index < 4:
            return
        beam = self.data["beams"][index]
        if key not in beam or key in {"phase", "delay", "taper"}:
            return
        if isinstance(beam[key], float):
            value = float(value)
            if not np.isfinite(value):
                return
        if key == "amplitude":
            value = min(1.0 if self.data["unlocked"] else 0.4, max(0.0, value))
        if key == "file":
            value = QUrl(value).toLocalFile() if str(value).startswith("file:") else value
        beam[key] = value
        self._queue("waveform")
        self.changed.emit()

    @pyqtSlot(int, float, float)
    def steer(self, index, u, v):
        if self.closing or self.data["exclusive"]:
            return
        norm = max(1.0, np.hypot(u, v))
        self.data["beams"][index].update(u=u / norm, v=v / norm)
        self._queue("waveform")
        self.changed.emit()

    @pyqtSlot(int, int, str, float)
    def setWeight(self, beam, sensor, key, value):
        if self.closing or self.data["exclusive"] or key not in {"phase", "delay", "taper"} or not np.isfinite(value):
            return
        if key == "taper" and not 0 <= value <= 1:
            return
        board, antenna = divmod(sensor, 8)
        self.data["beams"][beam][key][board][antenna] = value
        self._queue("waveform")
        self.changed.emit()

    @pyqtSlot(int, str)
    def setMode(self, sensor, mode):
        if self.all_tx or self.closing or self.data["exclusive"] or mode not in {"wifi", "iq_rx", "iq_tx"}:
            return
        if sensor < 0:
            self.data["modes"] = [[mode] * 8 for _ in self.layout.hosts]
        else:
            board, antenna = divmod(sensor, 8)
            self.data["modes"][board][antenna] = mode
        self.data["message"] = "Sensor modes changed. Calibrate again."
        self._queue("stop", "modes", "waveform")
        self.changed.emit()

    def _snapshot(self, prepare_waveform=True):
        beams = copy.deepcopy(self.data["beams"][: self.data["count"]])
        for beam in beams if prepare_waveform else []:
            waveform(beam, self.data["samples"])
            if beam["type"] == "file" and not beam["file"]:
                raise ValueError("Choose an I/Q file for each file beam.")
        rf = dict(
            frequency_hz=round(self.data["frequency"] * 1e6),
            rf_gain=dict(a=self.data["gain_a"], b=self.data["gain_b"]),
            bandwidth_mhz=self.data["bandwidth"],
            output=self.data["output"],
            steering=dict(phase_deg=[0] * 8, delay_samples=[0] * 8),
        )
        return (beams, self.data["samples"], self.data["frequency"] * 1e6), rf, copy.deepcopy(self.data["modes"])

    @pyqtSlot()
    def flush(self):
        if self.data["busy"] or not self.pending or self.closing or not self.data["authorized"]:
            return
        if "stop" in self.pending:
            self.pending.remove("stop")
            self._run(lambda: self.session.mute(True), lambda _: self.data.update(playing=False))
            return
        if "monitor" in self.pending:
            self.pending.remove("monitor")
            self.configureMonitor()
            return
        kinds, self.pending = self.pending, set()
        prepare_waveform = bool(kinds & {"waveform", "modes", "start", "calibrate"})
        try:
            snapshot, rf, modes = self._snapshot(prepare_waveform)
        except ValueError as error:
            self.pending.update(kinds - {"start", "calibrate"})
            if kinds & {"start", "calibrate"} or "Choose an I/Q file" not in str(error):
                self.error.emit(str(error))
            return
        route = self.data["route"]
        self.starting = "start" in kinds
        if "calibrate" in kinds:
            self.data.update(calibrating=True, message="Calibrating RX, then reciprocal TX links…")

        def apply():
            try:
                if "calibrate" in kinds:
                    self.session.mute(True)
                if "modes" in kinds or any(s["modes"] != m for s, m in zip(self.session.states, modes)):
                    self.session.modes(modes)
                if kinds & {"rf", "modes", "start", "calibrate"}:
                    self.session.settings(rf)
                elif not self.steering_cleared:
                    self.session.settings({"steering": rf["steering"]})
                if prepare_waveform:
                    self.session.apply(*snapshot)
                previews = {}
                for index, beam in enumerate(snapshot[0] if prepare_waveform else []):
                    if beam["type"] == "file":
                        stat = Path(beam["file"]).stat()
                        key = (beam["file"], snapshot[1], stat.st_mtime_ns, stat.st_size)
                        cached = self.file_previews.get(index)
                        if cached is None or cached[0] != key:
                            words = load_iq(beam["file"], snapshot[1]).astype(np.int32)
                            i, q = words & 1023, (words >> 10) & 1023
                            previews[index] = (key, (np.where(i >= 512, i - 1024, i) + 1j * np.where(q >= 512, q - 1024, q)) / 511)
                self.steering_cleared = True
                result = self.session.calibrate(route) if "calibrate" in kinds else None
                if "start" in kinds and not self.closing:
                    if not any("iq_tx" in m for m in modes):
                        raise ValueError("Select at least one IQ TX sensor.")
                    self.session.mute(False)
                states = self.session.read() if kinds & {"modes", "rf", "calibrate"} else None
                return states, result, previews
            except Exception as original:
                try:
                    self.session.mute(True)
                    original.tx_stopped = True
                except Exception as stop_error:
                    raise RuntimeError(f"{original}\nCould not confirm TX stop: {stop_error}") from original
                raise

        def finished(result):
            states, calibration, previews = result
            self.file_previews.update(previews)
            self.starting = False
            if states:
                self._status(states)
            if "calibrate" in kinds:
                self.data.update(calibrating=False, playing="start" in kinds and not self.closing, message=f"Calibrated · {calibration['phase_residual_deg']:.2f}° link residual")
                self._queue("monitor")
            elif "start" in kinds:
                self.data["playing"] = True

        self._run(apply, finished, exclusive="calibrate" in kinds)

    @pyqtSlot()
    def calibrate(self):
        self.pending.discard("start")
        self._queue("calibrate", "modes", "rf")

    @pyqtSlot()
    def toggleTx(self):
        if self.data["playing"] or self.starting or "start" in self.pending:
            self.pending.discard("start")
            self._queue("stop")
        else:
            self._queue("start")

    @pyqtSlot()
    def configureMonitor(self):
        if self.data["busy"] or not self.data["authorized"] or self.closing:
            return
        config = dict(
            enabled=True,
            gain_mode="manual",
            gain=self.data["monitor_gain"],
            frequency_hz=round(self.data["frequency"] * 1e6),
            sample_rate_hz=self.data["monitor_rate"],
            samples=self.data["monitor_samples"],
            interval_us=self.data["monitor_interval"],
            offset_us=self.data["monitor_offset"],
            bandwidth_mhz=self.data["monitor_bw"],
        )

        def finished(monitor):
            self.monitor = monitor
            self.data["monitor"] = monitor is not None

        self._run(lambda: self.session.configure_monitor(config), finished)

    def _monitor_frame(self):
        if self.monitor and self.monitor.latest is not None:
            samples, self.monitor.latest = self.monitor.latest, None
            self.monitorSamples.emit(samples)

    @pyqtSlot()
    def close(self):
        if self.closing:
            return
        self.closing = True
        self.data.update(closing=True, message="Stopping transmitters…")
        self.pending.clear()
        self.timer.stop()
        self.poll.stop()
        self.changed.emit()
        if not self.data["busy"]:
            self._shutdown()

    def _shutdown(self):
        self.shutdown_started = True

        def shutdown():
            if self.session:
                try:
                    self.session.close()
                finally:
                    self.session = None

        self._run(shutdown, exclusive=True)
