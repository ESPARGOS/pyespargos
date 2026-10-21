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
    GridLayout {
        columns: root.width < 900 ? 1 : 2
        Layout.fillWidth: true
        RowLayout {
            Label { text: "Beams" }
            SpinBox { from: 1; to: 4; value: root.state.count; enabled: !root.state.exclusive; onValueModified: root.controller.setSetting("count",value) }
            Label { text: "Samples"; leftPadding: 20 }
            ComboBox { model: [256,512,1024,2048,4096,8192]; currentIndex: model.indexOf(root.state.samples); enabled: !root.state.exclusive; onActivated: root.controller.setSetting("samples",Number(currentText)) }
        }
        RowLayout {
            Layout.fillWidth: true
            Switch {
                text: "Beam-squint correction"
                checked: root.state.beams[beamEditor.beamIndex].squint
                enabled: root.state.authorized && !root.state.exclusive && !root.state.closing
                onToggled: root.controller.setBeam(beamEditor.beamIndex,"squint",checked)
                ToolTip.visible: hovered
                ToolTip.text: "Correct frequency-dependent steering for Beam " + (beamEditor.beamIndex + 1)
            }
            Item { Layout.fillWidth: true }
            ToolButton {
                objectName: "txTransmissionIndicator"
                implicitWidth: 48
                implicitHeight: 48
                enabled: root.state.authorized && !root.state.exclusive && !root.state.closing
                Accessible.name: root.state.playing ? "Stop transmission" : "Start transmission"
                ToolTip.visible: hovered
                ToolTip.text: root.state.playing ? "Transmitting — click to stop" : "TX muted — click to start"
                onClicked: root.controller.toggleTx()
                contentItem: Canvas {
                    id: transmissionIcon
                    opacity: parent.enabled ? 1 : 0.4
                    onWidthChanged: requestPaint()
                    onHeightChanged: requestPaint()
                    Connections { target: root.controller; function onChanged() { transmissionIcon.requestPaint() } }
                    onPaint: {
                        let c = getContext("2d"), x = width/2, y = height/2;
                        c.reset();
                        c.strokeStyle = root.state.playing ? "#70cf8e" : "#9aa4b2";
                        c.fillStyle = c.strokeStyle;
                        c.lineWidth = 2;
                        c.lineCap = "round";
                        c.beginPath(); c.arc(x,y,3,0,2*Math.PI); c.fill();
                        for (let radius of [8,13]) {
                            c.beginPath(); c.arc(x,y,radius,-Math.PI/3,Math.PI/3); c.stroke();
                            c.beginPath(); c.arc(x,y,radius,2*Math.PI/3,4*Math.PI/3); c.stroke();
                        }
                        if (!root.state.playing) {
                            c.beginPath(); c.moveTo(x-14,y+14); c.lineTo(x+14,y-14); c.stroke();
                        }
                    }
                }
            }
            BusyIndicator { running: root.state.busy; implicitWidth: 28; implicitHeight: 28 }
        }
    }
    GridLayout {
        id: workspace
        readonly property bool wide: root.width >= 1200
        columns: wide ? 2 : 1
        Layout.fillWidth: true
        Layout.fillHeight: true
        columnSpacing: 24
        rowSpacing: 20
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 1000
            Layout.minimumHeight: beamEditor.Layout.minimumHeight + 32
            color: "#222a2f"
            border.color: "#405159"
            radius: 4
            IQTxBeamEditor {
                id: beamEditor
                anchors.fill: parent
                anchors.margins: 1
                controller: root.controller
            }
        }
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 320
            Layout.maximumWidth: workspace.wide ? 340 : Infinity
            Layout.minimumHeight: workspace.wide ? 560 : 480
            color: "#222a2f"
            border.color: "#405159"
            radius: 4
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 12
                spacing: 12
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "Reference monitor"; font.pixelSize: 18; Layout.fillWidth: true }
                    ToolButton {
                        objectName: "monitorToggle"
                        text: root.state.monitor_enabled ? "■" : "▶"
                        enabled: root.state.authorized && !root.state.exclusive && !root.state.closing
                        Accessible.name: root.state.monitor_enabled ? "Stop monitor" : "Start monitor"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name
                        onClicked: root.controller.toggleMonitor()
                    }
                }
                Label { text: "−90 … 0 dBFS"; color: "#9aa4b2"; Layout.alignment: Qt.AlignRight }
                IQTxPlot { controller: root.controller; monitor: true; Layout.fillWidth: true; Layout.fillHeight: true }
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: "#405159" }
                Label { text: "Spectrum / I / Q"; color: "#9aa4b2" }
                IQTxPlot { controller: root.controller; monitor: true; traces: true; Layout.fillWidth: true; Layout.fillHeight: true }
            }
        }
    }
}
