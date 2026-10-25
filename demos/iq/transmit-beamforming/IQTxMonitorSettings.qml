import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var controller
    readonly property var state: controller.state
    spacing: 16
    enabled: root.state.authorized && !root.state.exclusive && !root.state.closing

    Label { text: "Reference monitor"; font.pixelSize: 18; topPadding: 12 }
    ColumnLayout {
        Layout.fillWidth: true
        spacing: 8
        Label { text: "Receiver"; font.bold: true }
        GridLayout {
            Layout.fillWidth: true
            columns: 2
            columnSpacing: 12
            rowSpacing: 8
            Label { text: "Gain"; Layout.preferredWidth: 130 }
            RowLayout {
                Layout.fillWidth: true
                Slider {
                    objectName: "monitorGain"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 60
                    from: 0; to: 72; stepSize: 1
                    snapMode: Slider.SnapAlways
                    value: root.state.monitor_gain
                    onMoved: root.controller.setSetting("monitor_gain", Math.round(value))
                }
                Label { text: root.state.monitor_gain; Layout.preferredWidth: 24; horizontalAlignment: Text.AlignRight }
            }
            Label { text: "Rate (MSa/s)" }
            ComboBox {
                Layout.fillWidth: true
                model: [80,40,16]
                currentIndex: model.indexOf(root.state.monitor_rate/1e6)
                onActivated: root.controller.setSetting("monitor_rate",Number(currentText)*1e6)
            }
            Label { text: "Bandwidth (MHz)" }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                SpinBox {
                    objectName: "monitorBandwidth"
                    Layout.fillWidth: true
                    from: 12; to: 67
                    editable: true
                    enabled: root.state.monitor_bw !== 0
                    value: root.state.monitor_bw || 40
                    onValueModified: root.controller.setSetting("monitor_bw",value)
                }
                CheckBox {
                    text: "Automatic"
                    checked: root.state.monitor_bw === 0
                    onToggled: root.controller.setSetting("monitor_bw",checked ? 0 : 40)
                }
            }
        }
    }
    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: "#405159" }
    ColumnLayout {
        Layout.fillWidth: true
        spacing: 8
        Label { text: "Capture timing"; font.bold: true }
        GridLayout {
            Layout.fillWidth: true
            columns: 2
            columnSpacing: 12
            rowSpacing: 8
            Label { text: "Samples"; Layout.preferredWidth: 130 }
            ComboBox {
                Layout.fillWidth: true
                model: [256,512,1024,2048,4096]
                currentIndex: model.indexOf(root.state.monitor_samples)
                onActivated: root.controller.setSetting("monitor_samples",Number(currentText))
            }
            Label { text: "Interval (µs)" }
            IQTxNumber {
                Layout.fillWidth: true
                value: root.state.monitor_interval; minimum: 1000; decimals: 0
                onEdited: number => root.controller.setSetting("monitor_interval",number)
            }
            Label { text: "Offset (µs)" }
            IQTxNumber {
                Layout.fillWidth: true
                value: root.state.monitor_offset; minimum: 0; decimals: 0
                onEdited: number => root.controller.setSetting("monitor_offset",number)
            }
        }
    }
}
