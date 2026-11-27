"""Reusable asynchronous BLE receiver controls for Qt demos.

Owns the settings it changes and restores them on close. No camera, advertiser
selection, beamforming or rendering dependencies are required.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

from espargos.controller_reference import prepare_packet_reference
import logging
from PyQt6 import QtCore
from espargos.board_ble import BLECapability


class BLEController(QtCore.QObject):
    DEFAULT_CONFIG = dict(channel=37, gain=40, polarization=2, phy="LE1M", gain_mode="auto", decimation=0, reference_address=None)
    stateChanged = QtCore.pyqtSignal()
    configChanged = QtCore.pyqtSignal(dict)
    connected = QtCore.pyqtSignal()
    operationStarted = QtCore.pyqtSignal()
    _finished = QtCore.pyqtSignal(object, str)

    def __init__(self, config=None, *, board_index=0, buffering=None, logger=None, parent=None):
        super().__init__(parent)
        self._config = dict(self.DEFAULT_CONFIG, **(config or {}))
        self.board_index = board_index
        self.buffering = buffering
        self.logger = logger or logging.getLogger("demo.BLEController")
        self.pool = self.receiver = None
        self._last_calibrated = False
        self._saved_buffer_policy = None
        self._saved = {}
        self._started_processing = False
        self._closed = False
        self._ready = self._busy = self._calibrating = False
        self._status = "Not connected"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ble-control")
        self._finished.connect(self._complete)
        self._calibration_timer = QtCore.QTimer(self, interval=250)
        self._calibration_timer.timeout.connect(self._refresh_calibration)
        self._calibration_timer.start()

    @staticmethod
    def receiver_settings(config):
        settings = {key: config[key] for key in BLECapability.CONFIG_KEYS if key in config}
        settings.update(rx_gain=config["gain"], rf_switch=config["polarization"])
        return settings

    def _submit(self, action, status, *, calibrating=False):
        if self._closed or self._busy:
            return
        self._busy, self._calibrating, self._status = True, calibrating, status
        self.operationStarted.emit()
        self.stateChanged.emit()

        def run():
            result, error = None, ""
            try:
                result = action()
            except (ConnectionError, TimeoutError) as exception:
                error = str(exception)
                self.logger.error("BLE receiver operation failed: %s", error, exc_info=self.logger.isEnabledFor(logging.DEBUG))
            except Exception as exception:
                self.logger.exception("BLE receiver operation failed")
                error = str(exception)
            if not self._closed:
                self._finished.emit(result, error)

        self._executor.submit(run)

    @QtCore.pyqtSlot(object, str)
    def _complete(self, config, error):
        if self._closed:
            return
        if config is not None:
            self._config = dict(config)
            self.configChanged.emit(dict(config))
        connected = self.receiver is not None and not self._ready
        self._ready = self.receiver is not None
        self._busy = self._calibrating = False
        self._last_calibrated = self._ready and all(self.receiver.is_calibrated(i) for i in self._saved)
        if self._config["polarization"] in (0, 1):
            status = "RF input is isolated" if self._config["polarization"] == 0 else "RF input uses the reference network"
        else:
            status = "Receiving" if not self.needsCalibration else "Reference calibration is needed"
        self._status = error or status
        self.stateChanged.emit()
        if connected:
            self.connected.emit()

    def start(self, pool_factory, *, calibrate=True):
        """Open a pool supplied by a factory on the control worker, then configure.

        Passing ``lambda: pool`` attaches an existing pool. This controller does
        not close the pool or stop processing that was already running.
        """
        if self.pool is not None or self._busy or self._closed:
            return

        calibrate = calibrate and self.calibrationSupported

        def connect():
            from demos.ble.common.application import ESPARGOSBluetoothApplication

            self.pool = pool_factory()
            self.pool.open()
            indices = range(len(self.pool.boards)) if self.board_index is None else [self.board_index]
            for i in indices:
                board = self.pool.boards[i]
                self._saved[i] = (board.get_controller_mode(), board.iq.get_config()["modes"], board.ble.get_config())
            ESPARGOSBluetoothApplication.request_mode(self.pool, boards=[self.pool.boards[i] for i in indices])
            self.receiver = self.pool.ble
            if self.buffering is not None:
                self._saved_buffer_policy = self.receiver.buffer_policy
                self.receiver.configure_buffering(self.buffering)
            self.receiver.apply_config(self.receiver_settings(self._config), board_index=self.board_index)
            if not self.pool.processing:
                self.pool.start_processing()
                self._started_processing = True
            if calibrate:
                self._calibrate()
            return dict(self._config)

        self._submit(connect, "Connecting and calibrating" if calibrate else "Connecting", calibrating=calibrate)

    def _refresh_calibration(self):
        if self._busy or not self._ready:
            return
        calibrated = all(self.receiver.is_calibrated(i) for i in self._saved)
        if calibrated != self._last_calibrated:
            self._last_calibrated = calibrated
            if self._status in ("Receiving", "Reference calibration is needed"):
                self._status = "Receiving" if calibrated else "Reference calibration is needed"
            self.stateChanged.emit()

    @QtCore.pyqtSlot("QVariantMap")
    def configureReceiver(self, settings):
        if not self._ready or self._busy or self._closed:
            return
        config = dict(self._config, **settings)
        if (
            config["channel"] not in range(40)
            or not 0 <= config["gain"] <= 76
            or config["polarization"] not in range(5)
            or config["phy"] not in ("LE1M", "LE2M")
            or config["gain_mode"] not in ("manual", "auto")
            or config["decimation"] not in (0, 2, 4, 8)
        ):
            return

        if config["phy"] == "LE2M" and config["decimation"] == 8:
            config["decimation"] = 0

        def apply():
            self.receiver.apply_config(self.receiver_settings(config), board_index=self.board_index)
            return config

        self._submit(apply, "Applying receiver settings")

    @QtCore.pyqtSlot()
    def calibrate(self):
        if self._ready and self.calibrationSupported:
            self._submit(self._calibrate, "Calibrating from reference packets", calibrating=True)

    def _calibrate(self):
        address = self._config.get("reference_address")
        external = [i for i in self._saved if self._saved[i][2].get("reference_source") == 2]
        masters = [i for i in self._saved if self._saved[i][2].get("reference_source") == 1]
        # A master in the aperture supplies the external-reference boards.
        # Restore its antenna route before measuring its own calibration.
        with ExitStack() as restore:
            if external and len(masters) == 1 and self.pool.reference_controller is None and address in (None, self._saved[masters[0]][2].get("reference_address")):
                if self._config["phy"] != "LE1M":
                    raise ValueError("Use an external LE2M reference advertiser for LE2M calibration")
                master = self.pool.boards[masters[0]]
                address = prepare_packet_reference(restore, master, "ble", self._config["channel"])
            for i in external:
                self.receiver.calibrate(board_index=i, reference_address=address)
        for i in self._saved:
            if i not in external:
                self.receiver.calibrate(board_index=i, reference_address=self._config.get("reference_address"))

    def report_error(self, message):
        if not self._closed and message != self._status:
            self._status = message
            self.stateChanged.emit()

    @QtCore.pyqtProperty("QVariantMap", notify=stateChanged)
    def receiverConfig(self):
        return dict(self._config)

    @QtCore.pyqtProperty(bool, notify=stateChanged)
    def calibrationSupported(self):
        return self._config["phy"] in ("LE1M", "LE2M")

    @QtCore.pyqtProperty(bool, notify=stateChanged)
    def needsCalibration(self):
        return not self._last_calibrated

    @QtCore.pyqtProperty(bool, notify=stateChanged)
    def calibrating(self):
        return self._calibrating

    @QtCore.pyqtProperty(bool, notify=stateChanged)
    def busy(self):
        return self._busy

    @QtCore.pyqtProperty(bool, notify=stateChanged)
    def ready(self):
        return self._ready

    @QtCore.pyqtProperty(str, notify=stateChanged)
    def status(self):
        return self._status

    def close(self):
        """Wait for in-flight control, restore settings and reject late UI actions."""
        if self._closed:
            return
        self._closed = True
        self._calibration_timer.stop()
        self._executor.shutdown(wait=True, cancel_futures=True)
        errors = []
        if self._started_processing:
            try:
                self.pool.stop_processing()
            except Exception as error:
                errors.append(error)
        for i, (controller, modes, config) in self._saved.items():
            board = self.pool.boards[i]
            for restore in (lambda: board.ble.set_config({k: config[k] for k in board.ble.CONFIG_KEYS if k in config}), lambda: board.iq.set_config({"modes": modes}), lambda: board.set_controller_mode(controller)):
                try:
                    restore()
                except Exception as error:
                    errors.append(error)
            if self.receiver is not None:
                self.receiver.invalidate_calibration(i)
        if self._saved:
            self._saved.clear()
            try:
                self.pool._refresh_receivers()
            except Exception as error:
                errors.append(error)
        if self._saved_buffer_policy is not None:
            self.receiver.configure_buffering(self._saved_buffer_policy)
            self._saved_buffer_policy = None
        self._ready = False
        if errors:
            raise ExceptionGroup("Could not restore BLE receiver settings", errors)
