import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import ESPARGOS.Tx 1.0

ColumnLayout {
    id: root
    required property var controller
    readonly property var state: controller.state
    spacing: 20
    RowLayout {
        Label { text: "Beams" }
        SpinBox { from: 1; to: 4; value: root.state.count; enabled: !root.state.exclusive; onValueModified: root.controller.setSetting("count",value) }
        Label { text: "Samples"; leftPadding: 20 }
        ComboBox { model: [256,512,1024,2048,4096,8192]; currentIndex: model.indexOf(root.state.samples); enabled: !root.state.exclusive; onActivated: root.controller.setSetting("samples",Number(currentText)) }
        Item { Layout.fillWidth: true }
        Label { text: root.state.playing ? "Transmitting" : "TX muted"; color: root.state.playing ? "#70cf8e" : "#9aa4b2" }
        BusyIndicator { running: root.state.busy; implicitWidth: 28; implicitHeight: 28 }
    }
    RowLayout {
        visible: !root.state.authorized
        Label { text: root.state.message; Layout.fillWidth: true; wrapMode: Text.Wrap }
        Button { text: "Reconnect"; enabled: !root.state.busy; onClicked: root.controller.connectArray() }
        Repeater { model: root.state.hosts; delegate: Button { required property string modelData; text: modelData+" Settings"; onClicked: Qt.openUrlExternally("http://"+modelData+"/") } }
    }
    IQTxBeamEditor { controller: root.controller; Layout.fillWidth: true; Layout.fillHeight: true }
    GridLayout {
        columns: root.width < 900 ? 1 : 2
        Layout.fillWidth: true
        Layout.preferredHeight: columns === 1 ? 400 : 230
        columnSpacing: 16
        rowSpacing: 16
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 2
            color: "#101014"
            border.color: "#70cf8e"
            border.width: 2
            radius: 4
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 8
                spacing: 6
                RowLayout {
                    Label { text: "Reference monitor"; font.bold: true; color: "#70cf8e"; Layout.fillWidth: true }
                    Label { text: "−90 … 0 dBFS"; color: "#9aa4b2" }
                }
                IQTxPlot { controller: root.controller; monitor: true; Layout.fillWidth: true; Layout.fillHeight: true }
            }
        }
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 1
            color: "#101014"
            border.color: "#405159"
            radius: 4
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 8
                Label { text: "Spectrum / I / Q"; color: "#9aa4b2" }
                IQTxPlot { controller: root.controller; monitor: true; traces: true; Layout.fillWidth: true; Layout.fillHeight: true }
            }
        }
    }
}
