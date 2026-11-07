#!/usr/bin/env python
"""Shared lifecycle for single-advertiser BLE measurement demos."""
import signal
import time
from PyQt6 import QtCore

from demos.common.application import ESPARGOSApplication
from demos.common.latest_worker import LatestWorker
from demos.ble.common.controller import BLEController
from demos.ble.common.advertisers import Advertisers, advertiser_record
from espargos import BufferPolicy, Pool
from espargos.ble_receiver import BLECalibrationError


class BLEMeasurementApplication(ESPARGOSApplication):
    DEFAULT_CONFIG = {
        "receiver": BLEController.DEFAULT_CONFIG,
        "advertiser": "",
        "calibrated": True,
    }
    packetArrived = QtCore.pyqtSignal(dict)
    responseArrived = QtCore.pyqtSignal(object, float)
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, argv):
        super().__init__(argv)
        self._closing = False
        self._after = time.monotonic()
        self._response = None
        self._initial_advertiser = self.appconfig.get("advertiser").lower()
        self._selected = ""
        self.advertisers = Advertisers(self)
        self.advertisers.selectionChanged.connect(self._selection_changed)
        self.packetArrived.connect(self._observe)
        self.responseArrived.connect(self._received)
        self.controller = BLEController(self.appconfig.get("receiver"),
            buffering=BufferPolicy(capacity=256, max_age=.5, batch_size=64), parent=self)
        self.controller.connected.connect(self._connected)
        self.controller.operationStarted.connect(self._clear)
        self.controller.configChanged.connect(lambda config: self.appconfig.set({"receiver": config}))
        self.errorOccurred.connect(self.controller.report_error)
        self._worker = LatestWorker(self._process)
        self.appConfigChanged.connect(self._clear)
        self.initialize_qml(self.QML_FILE,
                            {"bleController": self.controller, "advertisers": self.advertisers})
        signal.signal(signal.SIGINT, lambda *_: self.quit())
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.advertisers.refresh)
        self._timer.start(200)

    def _open_pool(self):
        self.pool = Pool(self.get_initial_config("pool", "hosts"))
        if len(self.pool.hosts) != 1:
            raise ValueError("BLE measurement demos currently support one array")
        return self.pool

    def _connected(self):
        self.controller.receiver.add_callback(self._discover, lambda c: True)
        self.controller.receiver.add_callback(self._measure)
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

    def _clear(self, *_):
        self._after = time.monotonic()
        self._response = None
        self._worker.clear()

    def _measure(self, cluster):
        packet = cluster.packet
        identity = packet.address_type + ":" + (packet.advertiser or "")
        if not self.controller.busy and not cluster.is_calibration and self._selected in (identity, packet.advertiser):
            self._worker.submit(identity, (cluster, time.monotonic()))

    def _process(self, item):
        cluster, received = item
        if received < self._after:
            return
        try:
            response = self.process_cluster(cluster)
        except (BLECalibrationError, ValueError) as error:
            self.errorOccurred.emit(str(error))
            return
        self.responseArrived.emit(response, received)

    def process_cluster(self, cluster):
        """Compute one display result on the worker thread."""
        raise NotImplementedError

    def _received(self, response, received):
        if received >= self._after and time.monotonic()-received < 1:
            self._response = response
            self._received_at = received

    def exec(self):
        self.controller.start(self._open_pool)
        try:
            return super().exec()
        finally:
            self.onAboutToQuit()

    def onAboutToQuit(self):
        if getattr(self,"_closing",True):
            return
        self._closing = True
        try:
            self.controller.close()
        finally:
            self._worker.close()
            super().onAboutToQuit()
