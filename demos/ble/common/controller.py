"""Reusable asynchronous BLE receiver controls for Qt demos.

Owns the settings it changes and restores them on close. No camera, advertiser
selection, beamforming or rendering dependencies are required.
"""

from concurrent.futures import ThreadPoolExecutor
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
        self._callback_handle = None
        self._last_calibrated = None
        self._saved_buffer_policy = None
        self._saved = None
        self._started_processing = False
        self._closed = False
        self._ready = self._busy = self._calibrating = False
        self._status = "Not connected"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ble-control")
        self._finished.connect(self._complete)

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

        def connect():
            self.pool = pool_factory()
            self.pool.open()
            board = self.pool.boards[self.board_index]
            self._saved = (board.control.get_json("get_controller_mode"), board.iq.get_config()["modes"], board.ble.get_config())
            board.control.post_json("set_controller_mode", {"mode": "ble"})
            self.pool.select(boards=[board.host]).configure(mode="ble")
            self.receiver = self.pool.ble
            self._callback_handle = self.receiver.add_callback(self._observe_calibration, lambda cluster: True)
            if self.buffering is not None:
                self._saved_buffer_policy = self.receiver.buffer_policy
                self.receiver.configure_buffering(self.buffering)
            self.receiver.apply_config(self.receiver_settings(self._config), board_index=self.board_index)
            if not self.pool.processing:
                self.pool.start_processing()
                self._started_processing = True
            if calibrate and self.calibrationSupported:
                self.receiver.calibrate(board_index=self.board_index, reference_address=self._config.get("reference_address"))
            return dict(self._config)

        self._submit(connect, "Connecting and calibrating" if calibrate else "Connecting", calibrating=calibrate)

    def _observe_calibration(self, cluster):
        if self._closed or cluster.board_index != self.board_index:
            return
        calibrated = self.receiver.is_calibrated(self.board_index)
        if calibrated != self._last_calibrated:
            self._last_calibrated = calibrated
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
        self.receiver.calibrate(board_index=self.board_index, reference_address=self._config.get("reference_address"))

    def report_error(self, message):
        if not self._closed:
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
        return self.receiver is None or not self.receiver.is_calibrated(self.board_index)

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
        self._executor.shutdown(wait=True, cancel_futures=True)
        errors = []
        if self._callback_handle is not None:
            self.receiver.remove_callback(self._callback_handle)
            self._callback_handle = None
        if self._started_processing:
            try:
                self.pool.stop_processing()
            except Exception as error:
                errors.append(error)
        if self._saved is not None:
            controller, modes, config = self._saved
            self._saved = None
            board = self.pool.boards[self.board_index]
            for restore in (lambda: board.ble.set_config({k: config[k] for k in board.ble.CONFIG_KEYS if k in config}), lambda: board.iq.set_config({"modes": modes}), lambda: board.control.post_json("set_controller_mode", controller)):
                try:
                    restore()
                except Exception as error:
                    errors.append(error)
            if self.receiver is not None:
                self.receiver.invalidate_calibration(self.board_index)
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
