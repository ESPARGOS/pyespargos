#!/usr/bin/env python
"""Calibrated BLE packet phase in the configured antenna layout."""

from pathlib import Path
import sys
import numpy as np
from PyQt6 import QtCore

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from demos.ble.common.application import ESPARGOSBluetoothApplication
from demos.common.application import CombinedArrayMixin
from demos.common.phase_arrays import phase_arrays


class BLEPhaseApplication(CombinedArrayMixin, ESPARGOSBluetoothApplication):
    DEFAULT_CONFIG = {**ESPARGOSBluetoothApplication.DEFAULT_CONFIG, "backlog": {"size": 32, "max_age": 1.0}}
    QML_FILE = Path(__file__).with_name("Main.qml")
    updatePhases = QtCore.pyqtSignal(list)
    layoutChanged = QtCore.pyqtSignal()

    def __init__(self, argv):
        self._drawn = None
        super().__init__(argv)

    def _connected(self):
        self.layoutChanged.emit()
        super()._connected()

    def process_cluster(self, cluster):
        iq = self.controller.receiver.packet_iq(cluster, calibrated=self.appconfig.get("calibrated"))
        covariance, counts = iq.covariance()
        if not np.any(np.triu(counts, k=1)):
            return None
        board = cluster.board_index
        return board, covariance, counts, cluster.rf_switch_state[board].ravel()

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
        n = self.indexing_matrix.size
        panels = [dict(colors=[[0.20, 0.26, 0.30, 1.0]] * n, labels=["—"] * n) for _ in range(2 if split else 1)]
        for board in {record[0] for record in records}:
            covariance, counts, feeds = map(np.asarray, zip(*(record[1:] for record in records if record[0] == board)))
            cells = np.flatnonzero(self.indexing_matrix.ravel() // 8 == board)
            order = self.indexing_matrix.ravel()[cells] % 8
            local_panels = phase_arrays(covariance[:, order][:, :, order], counts[:, order][:, :, order], feeds[:, order], split)
            for panel, local in zip(panels, local_panels):
                for i, cell in enumerate(cells):
                    panel["colors"][cell] = local["colors"][i]
                    panel["labels"][cell] = local["labels"][i]
        self.updatePhases.emit(panels)


if __name__ == "__main__":
    sys.exit(BLEPhaseApplication(sys.argv).exec())
