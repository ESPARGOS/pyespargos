import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../common" as Common

Common.ESPARGOSApplication {
    id: window
    title: "MIMO Radar"
    width: 1500
    height: 900
    receiverDrawerLabel: "⚙ Radar"
    property real floorDb: 12
    property real ceilingDb: 24
    property bool linearScale: true
    property bool automaticMaximum: true
    readonly property real displayMaximumDb: Math.max(floorDb + 1, ceilingDb, automaticMaximum ? backend.maximumDb : ceilingDb)
    readonly property real displaySpanDb: displayMaximumDb - floorDb
    property bool fullVelocityRange: false
    readonly property real velocitySpan: fullVelocityRange ? backend.velocityLimit : Math.min(2, backend.velocityLimit)

    footer: ToolBar {
        background: Rectangle { color: "#19252b" }
        Label {
            objectName: "radarStatus"
            width: parent.width
            padding: 8
            leftPadding: 24
            text: backend.status
            wrapMode: Text.WordWrap
            color: "#bac6cc"
        }
    }

    receiverDrawerComponent: Component {
        Common.AppDrawer {
            id: radarDrawer
            implicitHeight: window.height - headerHeight - window.footer.height
            edge: Qt.LeftEdge
            title: "Radar"
            contentLayout.width: Math.max(0, radarDrawer.width - 40)
            Button {
                objectName: "mainTransmitButton"
                Layout.columnSpan: 2
                Layout.fillWidth: true
                text: backend.transmitting ? "■ Stop transmission" : "▶ Start transmission"
                enabled: !backend.busy
                onClicked: backend.command("run", backend.transmitting ? 0 : 1)
            }
            Label { text: "RX sample rate" }
            ComboBox {
                objectName: "sampleRateSelector"
                Layout.fillWidth: true
                model: backend.sampleRates
                currentIndex: backend.sampleRateIndex
                enabled: !backend.busy
                onActivated: backend.command("sample_rate", currentIndex)
            }
            Label { text: "TX antenna feed" }
            ComboBox {
                objectName: "txFeedSelector"
                Layout.fillWidth: true
                model: ["Left", "Right"]
                currentIndex: backend.txFeed
                enabled: !backend.busy
                onActivated: backend.command("tx_feed", currentIndex)
            }
            Label { text: "RX antenna feed" }
            ComboBox {
                objectName: "rxFeedSelector"
                Layout.fillWidth: true
                model: ["Left", "Right"]
                currentIndex: backend.rxFeed
                enabled: !backend.busy
                onActivated: backend.command("rx_feed", currentIndex)
            }
            Button {
                objectName: "calibrateButton"
                Layout.columnSpan: 2
                Layout.fillWidth: true
                text: "Calibrate RX + TX"
                enabled: !backend.busy
                onClicked: backend.command("calibrate", 0)
            }
        }
    }
    appDrawerComponent: Component {
        Common.AppDrawer {
            id: displayDrawer
            implicitHeight: window.height - headerHeight - window.footer.height
            title: "Radar display"
            contentLayout.width: Math.max(0, displayDrawer.width - 40)
            Label {
                Layout.preferredWidth: 105
                Layout.maximumWidth: 105
                text: "Color scale"
            }
            ComboBox {
                objectName: "colorScale"
                Layout.fillWidth: true
                model: ["dB", "Linear power"]
                currentIndex: window.linearScale ? 1 : 0
                onActivated: window.linearScale = currentIndex === 1
            }
            Label {
                Layout.preferredWidth: 105
                Layout.maximumWidth: 105
                text: "Color limits"
            }
            RowLayout {
                Layout.fillWidth: true
                Label { text: window.floorDb.toFixed(0) }
                RangeSlider {
                    id: colorLimits
                    objectName: "colorLimits"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    Accessible.name: "Color limits (dB above noise)"
                    from: 0; to: 70; stepSize: 1
                    first.value: window.floorDb
                    second.value: window.ceilingDb
                    first.onMoved: {
                        first.value = Math.min(first.value, second.value - 1)
                        window.floorDb = first.value
                    }
                    second.onMoved: {
                        second.value = Math.max(second.value, first.value + 1)
                        window.ceilingDb = second.value
                    }
                }
                Label { text: window.ceilingDb.toFixed(0) }
            }
            Label {
                Layout.columnSpan: 2
                text: "Color limits are in dB above noise."
                opacity: 0.7
            }
            Label {
                Layout.preferredWidth: 105
                Layout.maximumWidth: 105
                text: "Auto-adjust"
            }
            Switch {
                objectName: "automaticMaximum"
                Layout.alignment: Qt.AlignLeft
                Accessible.name: "Auto-adjust upper limit"
                checked: window.automaticMaximum
                onToggled: window.automaticMaximum = checked
            }
            Label {
                Layout.columnSpan: 2
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "Raise the upper color limit for stronger returns."
                opacity: 0.7
            }
            Label {
                Layout.preferredWidth: 105
                Layout.maximumWidth: 105
                text: "Velocity view"
            }
            ComboBox {
                Layout.fillWidth: true
                model: ["±2 m/s", "Full range"]
                onActivated: window.fullVelocityRange = currentIndex === 1
            }
        }
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 16
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 18
            RadarPlot {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 1
                title: "Range–Doppler"
                textureName: "doppler"
                rangeMaximum: backend.rangeMaximum
                axisLabel: "Radial velocity (m/s, positive receding)"
                minimum: -window.velocitySpan
                maximum: window.velocitySpan
                dataMinimum: backend.velocityMinimum
                dataMaximum: backend.velocityLimit
                floorDb: window.floorDb
                spanDb: window.displaySpanDb
                linearScale: window.linearScale
            }
            RadarPolarPlot {
                rangeMaximum: backend.rangeMaximum
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 1.25
                floorDb: window.floorDb
                spanDb: window.displaySpanDb
                linearScale: window.linearScale
            }
        }
        RowLayout {
            Layout.alignment: Qt.AlignHCenter
            spacing: 12
            Label { text: window.linearScale ? Math.pow(10, window.floorDb / 10).toFixed(1) : window.floorDb.toFixed(0) }
            ShaderEffect {
                Layout.preferredWidth: 260
                Layout.preferredHeight: 16
                property var source: legendSource
                property real floorDb: 0
                property real spanDb: 1
                property real xLow: 0
                property real xHigh: 1
                property real beamspacePeriod: backend.beamspacePeriod
                property real polar: 0
                property real legend: 1
                property real linearScale: window.linearScale ? 1 : 0
                vertexShader: "heatmap.vert.qsb"
                fragmentShader: "heatmap.frag.qsb"
            }
            Image { id: legendSource; visible: false; source: "image://radar/doppler/" + backend.revision }
            Label { text: window.linearScale ? Math.pow(10, (window.floorDb + window.displaySpanDb) / 10).toFixed(1) + " × estimated noise power" : (window.floorDb + window.displaySpanDb).toFixed(0) + " dB above estimated noise" }
        }
        Label {
            Layout.alignment: Qt.AlignHCenter
            text: "Move toward or away from the array. Stationary returns are suppressed."
            color: "#bac6cc"
        }
    }
}
