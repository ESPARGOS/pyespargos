"""The BLE UI controller works without a camera or QML application shell."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import time
import unittest
from unittest.mock import patch
from espargos import Pool, BufferPolicy
from test_ble_api import FakeBoard
from test_configuration import seed

try:
    from PyQt6 import QtWidgets
    from demos.ble.common.controller import BLEController
except ImportError:
    QtWidgets = None


@unittest.skipIf(QtWidgets is None, "Optional Qt dependencies unavailable")
class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def wait_idle(self, controller):
        deadline = time.monotonic() + 3
        while controller.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.001)
        self.assertFalse(controller.busy)

    def setup_controller(self, *, processing=False):
        board = FakeBoard()
        board.iq.set_config = lambda config: setattr(board, "modes", config["modes"])
        pool = Pool([board])
        pool.ble
        if processing:
            pool.start_processing()
        controller = BLEController(buffering=BufferPolicy(capacity=12))
        self.addCleanup(pool.close)
        self.addCleanup(controller.close)
        return board, pool, controller

    def test_settings_calibration_failure_and_late_actions(self):
        board, pool, controller = self.setup_controller()
        changed = []
        controller.configChanged.connect(changed.append)
        with patch("espargos.configuration._wait"):
            controller.start(lambda: pool, calibrate=False)
            self.wait_idle(controller)
        self.assertTrue(controller.ready)
        self.assertEqual(pool.ble.buffer_policy.capacity, 12)
        seed(pool.ble)
        controller.configureReceiver({"gain_mode": "auto", "polarization": 4})
        self.wait_idle(controller)
        self.assertFalse(controller.needsCalibration)
        self.assertEqual(changed[-1]["polarization"], 4)
        with patch.object(pool.ble, "calibrate", side_effect=RuntimeError("reference absent")):
            controller.calibrate()
            self.wait_idle(controller)
        self.assertEqual(controller.status, "reference absent")
        self.assertFalse(controller.calibrating)
        controller.close()
        self.assertFalse(pool.processing)
        self.assertEqual(pool.ble.buffer_policy, BufferPolicy())
        writes = len(board.writes)
        controller.configureReceiver({"gain": 55})
        controller.calibrate()
        self.app.processEvents()
        self.assertEqual(len(board.writes), writes)

    def test_attached_controller_preserves_existing_processing(self):
        board, pool, controller = self.setup_controller(processing=True)
        with patch("espargos.configuration._wait"):
            controller.start(lambda: pool, calibrate=False)
            self.wait_idle(controller)
        controller.close()
        self.assertTrue(pool.processing)

    def test_reboot_updates_calibration_state_without_camera(self):
        board, pool, controller = self.setup_controller()
        with patch("espargos.configuration._wait"):
            controller.start(lambda: pool, calibrate=False)
            self.wait_idle(controller)
        seed(pool.ble)
        controller._last_calibrated = True
        notifications = []
        controller.stateChanged.connect(lambda: notifications.append(controller.needsCalibration))
        for antenna in range(8):
            board.emit(antenna, clock_epoch=2, generation=board.config['generation'])
        deadline = time.monotonic()+1
        while not notifications and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.001)
        self.assertIn(True, notifications)
        self.assertEqual(len(pool.ble._cluster_callbacks), 1)
        controller.close()
        self.assertEqual(len(pool.ble._cluster_callbacks), 0)

    def test_start_failure_still_restores_snapshot(self):
        board, pool, controller = self.setup_controller()
        before = dict(board.config)
        with patch("espargos.configuration._wait"), patch.object(board.ble, "set_config", side_effect=OSError("lost reply")):
            controller.start(lambda: pool, calibrate=False)
            self.wait_idle(controller)
        self.assertEqual(controller.status, "lost reply")
        controller.close()
        self.assertEqual(board.config["gain_mode"], before["gain_mode"])
