"""Config delivery stays on the Qt thread, including during delayed acknowledgements."""

import json
import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
from PyQt6 import QtCore
from PyQt6.QtWidgets import QApplication

from demos.common.config_manager import ConfigManager


def test_force_waits_for_app_ack_and_coalesces_followup_edits():
    app = QApplication.instance() or QApplication([])
    manager = ConfigManager()
    owner = threading.get_ident()
    deliveries = []
    completed = []
    manager.updateAppState.connect(lambda delta: deliveries.append((threading.get_ident(), delta)), QtCore.Qt.ConnectionType.DirectConnection)
    manager.forceConfigAppApplied.connect(lambda: completed.append(True))
    manager.force({"gain": 10})
    app.processEvents()
    assert deliveries == [(owner, {"gain": 10})]

    for gain in (12, 14):
        manager.setConfigFromUI(json.dumps({"gain": gain}))
        app.processEvents()
    assert len(deliveries) == 1
    assert not completed

    manager.updateAppStateHandled.emit()
    app.processEvents()
    assert completed == [True]
    assert deliveries[1:] == [(owner, {"gain": 14})]


def test_worker_setting_delivers_on_owner_thread_and_parent_can_close_without_ack():
    app = QApplication.instance() or QApplication([])
    parent = QtCore.QObject()
    manager = ConfigManager(parent=parent)
    delivered = []
    manager.updateUIState.connect(lambda data: delivered.append((threading.get_ident(), json.loads(data))), QtCore.Qt.ConnectionType.DirectConnection)
    worker = threading.Thread(target=manager.set, args=({"gain": 20},))
    worker.start()
    worker.join()
    for _ in range(3):
        app.processEvents()
    assert delivered == [(threading.get_ident(), {"gain": 20})]
    manager.set({"gain": 30})
    parent.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    assert len(delivered) == 1
