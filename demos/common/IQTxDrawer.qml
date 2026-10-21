import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

Drawer {
    id: root
    required property var controller
    readonly property var state: controller.state
    property int headerHeight: 0
    implicitWidth: 420
    height: parent ? parent.height - headerHeight : 0
    y: headerHeight
    edge: Qt.LeftEdge
    modal: false
    dragMargin: 50
    background: Rectangle { color: "#222a2f" }
    ScrollView {
        anchors.fill: parent
        anchors.margins: 20
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
            width: parent.width
            spacing: 12
            IQTxCalibrationSettings { controller: root.controller; Layout.fillWidth: true }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: "#405159" }
            RowLayout {
                Layout.fillWidth: true
                Label { text: "Transmitter settings"; font.pixelSize: 18; Layout.fillWidth: true }
                ToolButton {
                    id: powerLock
                    objectName: "txPowerLock"
                    enabled: !root.state.exclusive && !root.state.closing
                    Accessible.name: root.state.unlocked ? "Lock recommended power limits" : "Unlock extended power range"
                    onClicked: root.controller.setSetting("unlocked", !root.state.unlocked)
                    ToolTip.visible: hovered
                    ToolTip.text: "Conservative limits for simultaneous TX: amplitude ≤ 0.4, gain A ≤ 2, gain B = 0. Unlock for higher settings, which can reset the arrays under load."
                    contentItem: Canvas {
                        id: lockIcon
                        implicitWidth: 24
                        implicitHeight: 24
                        onWidthChanged: requestPaint()
                        onHeightChanged: requestPaint()
                        Connections { target: root.controller; function onChanged() { lockIcon.requestPaint() } }
                        onPaint: {
                            let c = getContext("2d"); c.reset();
                            c.strokeStyle = root.state.unlocked ? "#ef6666" : "white";
                            c.lineWidth = 2;
                            let x=width/2, y=height/2;
                            c.strokeRect(x-7,y-1,14,11);
                            c.beginPath(); c.moveTo(x-4,y-1); c.lineTo(x-4,y-6);
                            c.arc(x,y-6,4,Math.PI,0);
                            if (!root.state.unlocked) c.lineTo(x+4,y-1);
                            c.stroke();
                            c.beginPath(); c.moveTo(x,y+3); c.lineTo(x,y+7); c.stroke();
                        }
                    }
                }
            }
            GridLayout {
                columns: 2
                Layout.fillWidth: true
                enabled: root.state.authorized && !root.state.exclusive && !root.state.closing
                Label { text: "Frequency (MHz)" }
                IQTxNumber { value: root.state.frequency; minimum: 2300; maximum: 2600; onEdited: number => root.controller.setSetting("frequency",number) }
                Label { text: "RF gain A" }
                RowLayout {
                    Layout.fillWidth: true
                    Slider { objectName: "txGainA"; Layout.fillWidth: true; from: 0; to: root.state.unlocked ? 7 : 2; stepSize: 1; snapMode: Slider.SnapAlways; value: root.state.gain_a; onMoved: root.controller.setSetting("gain_a", Math.round(value)) }
                    Label { text: root.state.gain_a; Layout.preferredWidth: 18 }
                }
                Label { text: "RF gain B" }
                RowLayout {
                    Layout.fillWidth: true
                    Slider { objectName: "txGainB"; Layout.fillWidth: true; from: 0; to: 7; enabled: root.state.unlocked; stepSize: 1; snapMode: Slider.SnapAlways; value: root.state.gain_b; onMoved: root.controller.setSetting("gain_b", Math.round(value)) }
                    Label { text: root.state.gain_b; Layout.preferredWidth: 18 }
                }
                Label { text: "Bandwidth (MHz)" }
                ComboBox {
                    objectName: "txBandwidth"
                    Layout.fillWidth: true
                    property var bandwidths: Array.from(new Set([0,13,20,40,60,root.state.bandwidth])).sort((a,b) => a-b)
                    model: bandwidths.map(value => ({label: value === 0 ? "Open" : String(value), value: value}))
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: bandwidths.indexOf(root.state.bandwidth)
                    onActivated: root.controller.setSetting("bandwidth",currentValue)
                }
                Label { text: "Output" }
                ComboBox { Layout.fillWidth: true; model: ["Reference", "Antenna R", "Antenna L", "Isolation"]; property var ports: ["reference", "antenna_r", "antenna_l", "isolated"]; currentIndex: ports.indexOf(root.state.output); onActivated: root.controller.setSetting("output",ports[currentIndex]) }
            }
            Button {
                text: root.state.playing ? "■ Stop transmission" : "▶ Start transmission"
                Layout.fillWidth: true
                enabled: root.state.authorized && !root.state.exclusive && !root.state.closing
                onClicked: root.controller.toggleTx()
            }
        }
    }
}
