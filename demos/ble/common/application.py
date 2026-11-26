"""Shared application and packet processing for Bluetooth demos."""

from collections import deque
import signal
import time
import numpy as np
from PyQt6 import QtCore

from demos.common.application import ESPARGOSApplication
from demos.common.latest_worker import LatestWorker
from demos.ble.common.controller import BLEController
from demos.ble.common.advertisers import Advertisers, advertiser_record
from espargos import BufferPolicy
from espargos.ble_receiver import BLECalibrationError


class ESPARGOSBluetoothApplication(ESPARGOSApplication):
    CONTROLLER_MODE = SENSOR_MODE = "ble"

    DEFAULT_CONFIG = {
        "receiver": BLEController.DEFAULT_CONFIG,
        "advertiser": "",
        "min_antennas": 2,
        "backlog": {"size": 8, "max_age": 0.5},
        "calibrated": True,
    }
    packetArrived = QtCore.pyqtSignal(dict)
    responseArrived = QtCore.pyqtSignal(object, float)
    errorOccurred = QtCore.pyqtSignal(str, float)

    def __init__(self, argv, argparse_parent=None):
        super().__init__(argv, argparse_parent=argparse_parent)
        self._closing = False
        self._initial_advertiser = self.appconfig.get("advertiser")
        if self._initial_advertiser is not None:
            self._initial_advertiser = self._initial_advertiser.lower()
        config = self.appconfig.get("receiver")
        selected = config.pop("advertisers", ())
        self.advertisers = Advertisers(self, (address.lower() for address in selected))
        self.advertisers.selectionChanged.connect(self._selection_changed)
        self.packetArrived.connect(self._observe)
        self.controller = BLEController(config, buffering=BufferPolicy(capacity=256, max_age=0.5, batch_size=64), logger=self.logger, parent=self)
        self.controller.connected.connect(self._connected)
        self.controller.operationStarted.connect(self._clear)
        self.controller.configChanged.connect(self._receiver_config_changed)
        self.controller.stateChanged.connect(self.progressChanged)
        self.receiver = None
        self._worker = LatestWorker(self._process)
        extra_context = self._initialize_display()
        self.initialize_qml(self.QML_FILE, {"bleController": self.controller, "advertisers": self.advertisers, **extra_context})
        signal.signal(signal.SIGINT, lambda *_: QtCore.QMetaObject.invokeMethod(self, "quit", QtCore.Qt.ConnectionType.QueuedConnection))
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.advertisers.refresh)
        self._timer.start(200)

    def _initialize_display(self):
        """Keep recent responses from one selected advertiser for the chart demos."""
        self._after = self._received_at = time.monotonic()
        self._history = deque()
        self._selected = ""
        self.responseArrived.connect(self._received)
        self.errorOccurred.connect(self._report_error)
        self.appConfigChanged.connect(self._settings_changed)
        return {}

    def _receiver_config_changed(self, config):
        self.appconfig.set({"receiver": config})

    def _connected(self):
        self.receiver = self.controller.receiver
        self.receiver.settle_timeout = 0.05
        self.receiver.add_callback(self._discover, lambda c: True)
        self.receiver.add_callback(self._measure, include_partial=True)
        self.ready = True
        self.initComplete.emit()

    def _discover(self, cluster):
        packet = cluster.packet
        if packet.is_advertisement and packet.rf_switch != 1:
            self.packetArrived.emit(advertiser_record(packet))

    def _observe(self, record):
        self.advertisers.observe(record)
        if self._initial_advertiser is not None and self._initial_advertiser in ("", record["address"], record["identity"]):
            self._initial_advertiser = None
            self.advertisers.selectOnly(record["identity"])

    def _selection_changed(self):
        self._selected = next(iter(self.advertisers.selected), "")
        self._clear()

    def _settings_changed(self, changes):
        if changes.keys() - {"backlog"}:
            self._clear()
        else:
            self._trim_history()

    def _clear(self, *_):
        self._after = time.monotonic()
        self._history.clear()
        self._worker.clear()

    def _measure(self, cluster):
        packet = cluster.packet
        identity = packet.address_type + ":" + (packet.advertiser or "")
        if self.controller.busy or cluster.is_calibration or not packet.is_advertisement or self._selected not in (identity, packet.advertiser) or np.count_nonzero(np.isfinite(cluster.rx_gain)) < self.appconfig.get("min_antennas"):
            return
        self._worker.submit(identity, (cluster, time.monotonic() - cluster.age))

    def _process(self, item):
        cluster, received = item
        if received < self._after or time.monotonic() - received >= self.appconfig.get("backlog", "max_age"):
            return
        try:
            response = self.process_cluster(cluster)
        except (BLECalibrationError, ValueError) as error:
            self.errorOccurred.emit(str(error), received)
            return
        if response is not None:
            self.responseArrived.emit(response, received)

    def _report_error(self, message, received):
        if received >= self._after and not self.controller.busy:
            self.controller.report_error(message)

    def process_cluster(self, cluster):
        """Compute one display result on the worker thread."""
        raise NotImplementedError

    def _received(self, response, received):
        if received >= max(self._after, self._received_at) and time.monotonic() - received < self.appconfig.get("backlog", "max_age"):
            self._history.append((received, response))
            self._trim_history()
            self._received_at = received

    def _trim_history(self):
        cutoff = time.monotonic() - self.appconfig.get("backlog", "max_age")
        size = max(1, int(self.appconfig.get("backlog", "size")))
        while self._history and (len(self._history) > size or self._history[0][0] <= cutoff):
            self._history.popleft()

    def onAboutToQuit(self):
        if getattr(self, "_closing", True):
            return
        self._closing = True
        try:
            self.controller.close()
        finally:
            self._worker.close()
            super().onAboutToQuit()

    def _open_pool(self):
        self._prepare_pool_init({})
        if not hasattr(self, "pool"):
            self.pool = self._make_pool()
        if len(self.pool.hosts) != 1:
            raise ValueError("BLE demos currently support one eight-sensor board")
        return self.pool

    def _startup_pending(self):
        return self.controller.busy and not self.controller.ready

    def _calibration_message(self):
        return self.controller.status if self.controller.calibrating else ""

    def exec(self):
        self.controller.start(self._open_pool)
        try:
            return super().exec()
        finally:
            self.onAboutToQuit()
