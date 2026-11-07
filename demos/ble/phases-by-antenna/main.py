#!/usr/bin/env python
"""Calibrated BLE packet phase in the configured antenna layout."""
from collections import deque
from pathlib import Path
import sys
import time
import numpy as np
from PyQt6 import QtCore

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from demos.ble.common.application import BLEMeasurementApplication
from demos.common.application import CombinedArrayMixin
from demos.common.phase_arrays import phase_arrays


class BLEPhaseApplication(CombinedArrayMixin, BLEMeasurementApplication):
    QML_FILE = Path(__file__).with_name("Main.qml")
    updatePhases = QtCore.pyqtSignal(list)
    layoutChanged = QtCore.pyqtSignal()

    def __init__(self, argv):
        self._history = deque(maxlen=32)
        super().__init__(argv)

    def _open_pool(self):
        self._prepare_pool_init({})
        if len(self.pool.hosts) != 1:
            raise ValueError("BLE phase measurements currently support one board, including rotated layouts")
        return self.pool

    def _connected(self):
        self.layoutChanged.emit()
        super()._connected()

    def process_cluster(self, cluster):
        iq = self.controller.receiver.packet_iq(cluster, calibrated=self.appconfig.get("calibrated"))
        covariance, counts = iq.covariance()
        order = self.indexing_matrix.ravel()
        return covariance[np.ix_(order, order)], counts[np.ix_(order, order)], cluster.rf_switch_state.ravel()[order]

    def _clear(self, *_):
        super()._clear()
        self._history.clear()

    def _received(self, response, received):
        super()._received(response, received)
        if self._response is response:
            self._history.append((received, response))

    @QtCore.pyqtProperty(int, notify=layoutChanged)
    def numberOfRows(self):
        return getattr(self, "n_rows", 2)

    @QtCore.pyqtProperty(int, notify=layoutChanged)
    def numberOfColumns(self):
        return getattr(self, "n_cols", 4)

    @QtCore.pyqtSlot()
    def updateRequest(self):
        split = self.controller.receiverConfig["polarization"] == 4
        while self._history and time.monotonic() - self._history[0][0] >= 1:
            self._history.popleft()
        if not self._history:
            self.updatePhases.emit([{}] * (2 if split else 1))
            return
        # Random packets observe only part of each feed. Accumulate pairwise
        # measurements without mixing packet-wide phase gauges or feed domains.
        records = [record for _, record in self._history] if split else [self._history[-1][1]]
        covariance, counts, feeds = map(np.asarray, zip(*records))
        self.updatePhases.emit(phase_arrays(covariance, counts, feeds, split))


if __name__ == "__main__":
    sys.exit(BLEPhaseApplication(sys.argv).exec())
