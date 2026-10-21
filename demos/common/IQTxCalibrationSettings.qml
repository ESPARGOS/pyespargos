import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var controller
    readonly property var state: controller.state
    spacing: 8
    Label { text: "Calibration"; font.pixelSize: 18; topPadding: 12 }
    ComboBox {
        objectName: "txCalibrationMethod"
        Layout.fillWidth: true
        model: ["Controller monitor", "Reciprocal sensor links"]
        property var methods: ["monitor", "reciprocity"]
        currentIndex: methods.indexOf(root.state.calibration_method)
        enabled: !root.state.busy && !root.state.calibrating && !root.state.closing
        onActivated: root.controller.setSetting("calibration_method", methods[currentIndex])
    }
    ComboBox {
        objectName: "txCalibrationPath"
        Layout.fillWidth: true
        visible: root.state.calibration_method === "reciprocity"
        model: ["Reference network", "Right antenna feed", "Left antenna feed"]
        property var paths: ["reference", "antenna_r", "antenna_l"]
        currentIndex: paths.indexOf(root.state.calibration_path)
        enabled: !root.state.busy && !root.state.calibrating && !root.state.closing
        onActivated: root.controller.setSetting("calibration_path", paths[currentIndex])
    }
    Label {
        text: root.state.calibration_method === "monitor"
            ? "Requires a bidirectional REF path to the controller."
            : "Only a one-way REF link from controller to sensors is required: the sensors measure each other for TX calibration."
        Layout.fillWidth: true
        wrapMode: Text.Wrap
    }
    Button {
        objectName: "txCalibrate"
        text: root.state.calibrating ? "Calibrating…" : "Calibrate RX + TX"
        Layout.fillWidth: true
        Material.background: root.state.calibrated ? "#227b3d" : "#c53d3d"
        Material.foreground: "#ffffff"
        enabled: root.state.authorized && !root.state.busy && !root.state.closing
        onClicked: root.controller.calibrate()
    }
    Label { text: root.state.message; Layout.fillWidth: true; wrapMode: Text.Wrap }
    ProgressBar { Layout.fillWidth: true; indeterminate: true; visible: root.state.exclusive }
}
