#!/usr/bin/env python
"""Calibrated BLE packet phase in the configured antenna layout."""

from pathlib import Path
import sys
import numpy as np
from PyQt6 import QtCore

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from demos.ble.common.application import BLEMeasurementApplication
from demos.common.application import CombinedArrayMixin
from demos.common.phase_arrays import phase_arrays


class BLEPhaseApplication(CombinedArrayMixin, BLEMeasurementApplication):
    DEFAULT_CONFIG = {**BLEMeasurementApplication.DEFAULT_CONFIG, "backlog": {"size": 32, "max_age": 1.0}}
    QML_FILE = Path(__file__).with_name("Main.qml")
    updatePhases = QtCore.pyqtSignal(list)
    layoutChanged = QtCore.pyqtSignal()

    def __init__(self, argv):
        self._drawn = None
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
        if not np.any(np.triu(counts, k=1)):
            return None
        order = self.indexing_matrix.ravel()
        return covariance[np.ix_(order, order)], counts[np.ix_(order, order)], cluster.rf_switch_state.ravel()[order]

    def _clear(self, *_):
        super()._clear()
        self._drawn = None

    @QtCore.pyqtProperty(int, notify=layoutChanged)
    def numberOfRows(self):
        return getattr(self, "n_rows", 2)

    @QtCore.pyqtProperty(int, notify=layoutChanged)
    def numberOfColumns(self):
        return getattr(self, "n_cols", 4)

    @QtCore.pyqtSlot()
    def updateRequest(self):
        split = self.controller.receiverConfig["polarization"] == 4
        self._trim_history()
        records = self._history
        key = (split, tuple(stamp for stamp, _ in records))
        if key == self._drawn:
            return
        self._drawn = key
        if not records:
            self.updatePhases.emit([{}] * (2 if split else 1))
            return
        records = [record for _, record in records]
        covariance, counts, feeds = map(np.asarray, zip(*records))
        self.updatePhases.emit(phase_arrays(covariance, counts, feeds, split))


if __name__ == "__main__":
    sys.exit(BLEPhaseApplication(sys.argv).exec())
