"""WiFi IQ controls for a single-board camera or other calibrated CSI display."""

from concurrent.futures import ThreadPoolExecutor
import logging
import re

from PyQt6 import QtCore
from espargos import MACFilter


class WiFiIQControls(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    settingsChanged = QtCore.pyqtSignal()
    estimatorChanged = QtCore.pyqtSignal()
    filterChanged = QtCore.pyqtSignal()
    acquisitionChanged = QtCore.pyqtSignal()
    _finished = QtCore.pyqtSignal(object, str)

    def __init__(self, pool, parent=None):
        super().__init__(parent)
        self.pool = pool
        self.board = pool.boards[0]
        self._settings = {}
        self._backlog = None
        self._filter = None
        self._mac = ""
        self._busy = False
        self._status = ""
        self._pending = {}
        self._calibrated = False
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wifi-iq-control")
        self._finished.connect(self._complete)
        self._calibration_timer = QtCore.QTimer(self, interval=250)
        self._calibration_timer.timeout.connect(self._refresh_calibration)
        self._calibration_timer.start()

    @QtCore.pyqtProperty(bool, notify=changed)
    def busy(self):
        return self._busy

    @QtCore.pyqtProperty(str, notify=changed)
    def status(self):
        return self._status

    @QtCore.pyqtProperty("QVariantMap", notify=settingsChanged)
    def settings(self):
        return self._settings

    @QtCore.pyqtProperty(bool, notify=changed)
    def calibrated(self):
        return self._calibrated

    @QtCore.pyqtProperty(str, notify=estimatorChanged)
    def estimator(self):
        return self.pool.wifi_iq.estimator

    @QtCore.pyqtSlot(str)
    def setEstimator(self, mode):
        self.pool.wifi_iq.estimator = mode
        self.acquisitionChanged.emit()
        self.estimatorChanged.emit()

    def _submit(self, settings, calibrate=False):
        self._busy = True
        self._status = "Measuring the phase reference…" if calibrate else ""
        self.changed.emit()
        return self._executor.submit(self._configure, settings, calibrate)

    def _configure(self, settings, calibrate):
        actual, error = None, ""
        try:
            if settings:
                actual = self.pool.wifi_iq.configure(settings)
            if calibrate:
                self.pool.wifi_iq.calibrate()
        except Exception as exc:
            error = str(exc)
            logging.getLogger(__name__).exception("WiFi IQ configuration or calibration failed")
        finally:
            if actual is None or calibrate:
                try:
                    actual = self.board.wifi_iq.get_config()
                except Exception as exc:
                    error = error or str(exc)
            self._finished.emit(actual, error or ("Calibrated" if calibrate else "Configured"))

    @QtCore.pyqtSlot(object, str)
    def _complete(self, config, status):
        if self._closed:
            return
        self._busy = False
        self._status = status
        if config is not None:
            self._settings = config
        self._calibrated = self.pool.wifi_iq.has_calibration()
        if self._pending:
            pending, self._pending = self._pending, {}
            self._submit(pending)
            return
        self.settingsChanged.emit()
        self.acquisitionChanged.emit()
        self.changed.emit()

    def _refresh_calibration(self):
        if self._busy:
            return
        calibrated = self.pool.wifi_iq.has_calibration()
        if self._calibrated != calibrated:
            self._calibrated = calibrated
            self.changed.emit()

    def start(self, channel, rx_gain, *, gain_mode="manual", boot_sync=True, estimator="headers", rf_switch=2, calibrate=True):
        self.setEstimator(estimator)
        settings = dict(channel=channel, rx_gain=rx_gain, gain_mode=gain_mode, rf_switch=rf_switch, boot_sync=boot_sync)
        return self._submit(settings, calibrate)

    @QtCore.pyqtSlot("QVariantMap")
    def apply(self, settings):
        if self._closed:
            return
        if self._busy:
            self._pending.update(settings)
        else:
            self._submit(settings)

    @QtCore.pyqtSlot()
    def calibrate(self):
        if self._busy or self._closed:
            return
        if self._backlog is not None:
            self._backlog.apply_calibration = True
        self._submit({}, True)

    def attach_backlog(self, backlog):
        self._backlog = backlog
        self.acquisitionChanged.connect(backlog.clear)

    @QtCore.pyqtProperty(str, notify=filterChanged)
    def macFilter(self):
        return self._mac

    @QtCore.pyqtSlot(str)
    def setMacFilter(self, mac):
        address = mac.replace(":", "").strip().lower()
        if self._backlog is None or (address and not re.fullmatch("[0-9a-f]{12}", address)):
            return
        if self._filter is not None:
            self._backlog.remove_filter(self._filter)
        self._mac = bytes.fromhex(address).hex(":")
        self._filter = MACFilter(address + "$") if address else None
        if self._filter is not None:
            self._backlog.add_filter(self._filter)
        self.acquisitionChanged.emit()
        self.filterChanged.emit()

    def close(self):
        self._closed = True
        self._calibration_timer.stop()
        self._executor.shutdown(wait=True)
