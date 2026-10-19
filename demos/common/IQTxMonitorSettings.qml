import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var controller
    readonly property var state: controller.state
    Label { text: "Reference monitor"; font.pixelSize: 18; topPadding: 12 }
    GridLayout {
        columns: 2
        enabled: root.state.authorized && !root.state.exclusive && !root.state.closing
        Label { text: "Gain" }
        SpinBox { from: 0; to: 72; value: root.state.monitor_gain; onValueModified: root.controller.setSetting("monitor_gain",value) }
        Label { text: "Rate (MSa/s)" }
        ComboBox { model: [80,40,16]; currentIndex: model.indexOf(root.state.monitor_rate/1e6); onActivated: root.controller.setSetting("monitor_rate",Number(currentText)*1e6) }
        Label { text: "Samples" }
        ComboBox { model: [256,512,1024,2048,4096]; currentIndex: model.indexOf(root.state.monitor_samples); onActivated: root.controller.setSetting("monitor_samples",Number(currentText)) }
        Label { text: "Bandwidth (MHz)" }
        ComboBox { model: [12,20,40,60]; currentIndex: model.indexOf(root.state.monitor_bw); onActivated: root.controller.setSetting("monitor_bw",Number(currentText)) }
        Label { text: "Interval (µs)" }
        IQTxNumber { value: root.state.monitor_interval; minimum: 1000; decimals: 0; onEdited: number => root.controller.setSetting("monitor_interval",number) }
        Label { text: "Offset (µs)" }
        IQTxNumber { value: root.state.monitor_offset; minimum: 0; decimals: 0; onEdited: number => root.controller.setSetting("monitor_offset",number) }
    }
}
