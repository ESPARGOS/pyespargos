"""Bounded, pan/zoom TX source and reference-monitor plots for Qt Quick demos."""

import numpy as np
from PyQt6 import QtCore as C, QtGui as G
from PyQt6.QtQuick import QQuickPaintedItem
from waveform import synthesized_waveform
from demos.iq.common.iq_waterfall import WaterfallImageProvider, max_pool_to_width


class IQTxPlot(QQuickPaintedItem):
    controllerChanged = C.pyqtSignal()
    beamChanged = C.pyqtSignal()
    monitorChanged = C.pyqtSignal()
    tracesChanged = C.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._controller = None
        self._beam = 0
        self._monitor = False
        self._traces = False
        self.waterfall = WaterfallImageProvider(512, 160)
        self.signature = None
        self.z = np.zeros(256, complex)
        self.spectrum = np.full(512, -90.0)
        self.scale, self.start, self.last = 1.0, 0.0, None
        self.vertical_scale, self.center = 1.0, 0.0
        self.setAcceptedMouseButtons(C.Qt.MouseButton.LeftButton)
        self.setAcceptHoverEvents(True)

    @C.pyqtProperty(C.QObject, notify=controllerChanged)
    def controller(self):
        return self._controller

    @controller.setter
    def controller(self, value):
        if self._controller:
            try:
                self._controller.changed.disconnect(self.refresh)
                self._controller.monitorSamples.disconnect(self.monitor_samples)
            except RuntimeError:
                pass  # QML clears properties during engine teardown.
        self._controller = value
        if value:
            value.changed.connect(self.refresh)
            value.monitorSamples.connect(self.monitor_samples)
        self.controllerChanged.emit()
        self.refresh()

    @C.pyqtProperty(int, notify=beamChanged)
    def beamIndex(self):
        return self._beam

    @beamIndex.setter
    def beamIndex(self, value):
        self._beam = value
        self.reset_view()
        self.beamChanged.emit()
        self.refresh()

    @C.pyqtProperty(bool, notify=monitorChanged)
    def monitor(self):
        return self._monitor

    @monitor.setter
    def monitor(self, value):
        self._monitor = value
        self.monitorChanged.emit()
        self.refresh()

    @C.pyqtProperty(bool, notify=tracesChanged)
    def traces(self):
        return self._traces

    @traces.setter
    def traces(self, value):
        self._traces = value
        self.tracesChanged.emit()
        self.update()

    def monitor_samples(self, z):
        if self.monitor:
            self.set_samples(z)

    def refresh(self):
        if not self._controller or self.monitor:
            return
        state = self._controller.data
        beam, n = state["beams"][self._beam], state["samples"]
        # Steering must not recalculate the preview or read a file on the UI thread.
        signature = (n,) + tuple(beam[k] for k in ("type", "amplitude", "offset_mhz", "first_mhz", "second_mhz", "start_mhz", "stop_mhz", "duration_us", "delay_us", "file"))
        cached = self._controller.file_previews.get(self._beam)
        signature += (cached[0] if cached else None,)
        if signature == self.signature:
            return
        self.signature = signature
        try:
            kind = beam["type"]
            if kind == "file":
                # File samples are supplied by the worker after upload; never read disks here.
                z = cached[1] if cached and cached[0][:2] == (beam["file"], n) else np.zeros(n, complex)
            else:
                z = synthesized_waveform(beam).synthesize(n)
            self.set_samples(z * beam["amplitude"])
        except (ValueError, OSError):
            self.set_samples(np.zeros(256, complex))

    def set_samples(self, z):
        if len(z) != len(self.z):
            self.reset_view()
        self.z = np.asarray(z)
        if self.monitor:
            n = min(1024, len(z))
            db = 20 * np.log10(np.maximum(abs(np.fft.fftshift(np.fft.fft(z[:n] * np.hanning(n)))) / (512 * max(1, np.hanning(n).sum())), 1e-6))
            self.spectrum = max_pool_to_width(db, 512)
            if not self.traces:
                self.waterfall.add_power_db(db)
        self.update()

    def paint(self, p):
        width, height = self.width(), self.height()
        p.fillRect(C.QRectF(0, 0, width, height), G.QColor("#11191e"))
        if self.monitor:
            p.fillRect(C.QRectF(0, 0, width, height), G.QColor("#101014"))
            if not self.traces:
                image, _ = self.waterfall.requestImage("", C.QSize())
                p.drawImage(C.QRectF(0, 0, width, height), image)
                return
            p.setPen(G.QColor("#70cf8e"))
            self.line(p, np.clip((self.spectrum + 90) / 90, 0, 1), height * 0.43, height * 0.46, width)
            offset, span = height * 0.76, height * 0.21
        else:
            width, height = max(1, width - 40), max(1, height - 22)
            p.translate(36, 0)
            p.setPen(G.QColor("#405159"))
            count = self.view_count()
            for i in range(5):
                x = i * width / 4
                p.drawLine(C.QPointF(x, 0), C.QPointF(x, height))
                p.setPen(G.QColor("#9aa4b2"))
                p.drawText(C.QRectF(min(max(0, x - 24), width - 48), height + 2, 48, 18), C.Qt.AlignmentFlag.AlignHCenter, str(round(self.start + i * (count - 1) / 4)))
                p.setPen(G.QColor("#405159"))
            for i in (-1, 0, 1):
                y = height / 2 - i * height * 0.42
                p.drawLine(C.QPointF(0, y), C.QPointF(width, y))
                p.setPen(G.QColor("#9aa4b2"))
                p.drawText(C.QRectF(-36, y - 9, 32, 18), C.Qt.AlignmentFlag.AlignRight, f"{self.center+i/self.vertical_scale:.2g}")
                p.setPen(G.QColor("#405159"))
            p.setClipRect(C.QRectF(0, 0, width, height))
            offset, span = height / 2, height * 0.42 * self.vertical_scale
        count = max(8, int(len(self.z) / self.scale))
        start = min(max(0, int(self.start)), max(0, len(self.z) - count))
        z = self.z[start : start + count]
        if not self.monitor:
            z = (z.real - self.center) + 1j * (z.imag - self.center)
        peak = max(1e-6, abs(z.real).max(initial=0), abs(z.imag).max(initial=0)) if self.monitor else 1
        for values, color in ((z.real, "#60c7ef"), (z.imag, "#ffa760")):
            p.setPen(G.QColor(color))
            self.line(p, values / peak, span, offset, width)

    @staticmethod
    def line(p, values, span, offset, width):
        path = G.QPainterPath()
        for i, value in enumerate(values):
            point = C.QPointF(i * width / max(1, len(values) - 1), offset - float(value) * span)
            path.moveTo(point) if i == 0 else path.lineTo(point)
        p.drawPath(path)

    def view_count(self):
        return min(len(self.z), max(8, int(len(self.z) / self.scale)))

    def reset_view(self):
        self.scale, self.start = 1.0, 0.0
        self.vertical_scale, self.center = 1.0, 0.0
        self.update()

    def wheelEvent(self, event):
        if self.monitor and not self.traces:
            event.ignore()
            return
        delta = event.angleDelta().y() or event.pixelDelta().y()
        factor = 1.25 ** (delta / 120)
        if event.modifiers() & C.Qt.KeyboardModifier.ShiftModifier and not self.monitor:
            height = max(1, self.height() - 22)
            fraction = (height / 2 - event.position().y()) / (height * 0.42)
            anchor = self.center + fraction / self.vertical_scale
            self.vertical_scale = min(1000, max(0.1, self.vertical_scale * factor))
            self.center = anchor - fraction / self.vertical_scale
        else:
            width = max(1, self.width() - (0 if self.monitor else 40))
            x = event.position().x() - (0 if self.monitor else 36)
            fraction = min(1, max(0, x / width))
            anchor = self.start + fraction * (self.view_count() - 1)
            self.scale = min(max(1, len(self.z) / 8), max(1, self.scale * factor))
            count = self.view_count()
            self.start = min(max(0, anchor - fraction * (count - 1)), max(0, len(self.z) - count))
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        if self.monitor and not self.traces:
            event.ignore()
            return
        self.last = event.position()
        self.setKeepMouseGrab(True)
        event.accept()

    def mouseMoveEvent(self, event):
        if self.last is not None:
            width = max(1, self.width() - (0 if self.monitor else 40))
            self.start += (self.last.x() - event.position().x()) * (self.view_count() - 1) / width
            self.start = min(max(0, self.start), max(0, len(self.z) - self.view_count()))
            self.last = event.position()
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        self.last = None
        self.setKeepMouseGrab(False)
        event.accept()

    def mouseUngrabEvent(self):
        self.last = None
        self.setKeepMouseGrab(False)

    def mouseDoubleClickEvent(self, event):
        self.reset_view()
        event.accept()
