"""Shared QML receiver controls preserve firmware values across button order."""

import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt6 import QtCore, QtQml, QtQuick, QtWidgets
except ImportError:
    QtWidgets = None


@unittest.skipIf(QtWidgets is None, "Install the optional PyQt6 demo dependencies")
class ReceiverControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_ble_drawer_works_without_camera_or_backend(self):
        engine = QtQml.QQmlEngine()
        component = QtQml.QQmlComponent(engine)
        common = (Path(__file__).resolve().parents[1] / "demos/ble/common").as_uri()
        component.setData(('''
import QtQuick
import "''' + common + '''" as BLE
Item {
    property int edits: 0
    property int calibrations: 0
    BLE.BLERxDrawer {
        onConfigurationRequested: parent.edits++
        onCalibrationRequested: parent.calibrations++
    }
}
''').encode(), QtCore.QUrl())
        self.assertFalse(component.isError(), [e.toString() for e in component.errors()])
        root = component.create()
        self.assertIsNotNone(root)
        drawer = root.findChild(QtCore.QObject, "bleReceiverDrawer")
        drawer.setProperty("receiverConfig", dict(channel=38, gain=55, polarization=4,
            phy="LE2M", gain_mode="manual", decimation=2, capture_samples=2048))
        self.assertEqual(drawer.property("rfSwitchMode"), 4)
        self.assertEqual(root.property("edits"), 0)
        QtCore.QMetaObject.invokeMethod(root.findChild(QtCore.QObject, "bleCalibrate"), "clicked")
        self.assertEqual(root.property("calibrations"), 1)
        root.deleteLater()
        self.app.processEvents()

    def test_rf_values_and_programmatic_gain_do_not_apply_settings(self):
        engine = QtQml.QQmlEngine()
        component = QtQml.QQmlComponent(engine)
        common = (Path(__file__).resolve().parents[1] / "demos/common").as_uri()
        component.setData(('''
import QtQuick
import "''' + common + '''" as Common
Item {
    property int edits: 0
    Common.RFSwitchControl {
        objectName: "routes"
        modes: [0, 2, 3, 4, 1]
        onActivated: parent.edits++
    }
    Common.GainSlider {
        objectName: "gain"
        onEdited: parent.edits++
    }
}
''').encode(), QtCore.QUrl())
        self.assertFalse(component.isError(), [e.toString() for e in component.errors()])
        root = component.create()
        self.assertIsNotNone(root)
        routes = root.findChild(QtCore.QObject, "routes")
        gain = root.findChild(QtCore.QObject, "gain")
        buttons = {item.objectName(): item for item in routes.childItems() if item.objectName().startswith("rfSwitch")}
        gain.setProperty("value", 55)
        routes.setProperty("value", 1)
        self.assertEqual(root.property("edits"), 0)
        self.assertTrue(buttons["rfSwitch1"].property("checked"))
        for value in (0, 2, 3, 4, 1):
            button = buttons["rfSwitch" + str(value)]
            QtCore.QMetaObject.invokeMethod(button, "clicked")
            self.assertEqual(routes.property("value"), value)
            self.assertTrue(button.property("checked"))
        self.assertEqual(root.property("edits"), 5)
        self.assertEqual(gain.property("value"), 55)
        root.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
