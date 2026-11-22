import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

Button {
    property bool needCalibration: false
    Layout.columnSpan: 2
    Layout.alignment: Qt.AlignCenter
    text: "Trigger Calibration"
    Material.background: needCalibration ? "#7f1d1d" : ApplicationWindow.window.Material.background
    Material.foreground: "#ffffff"
}
