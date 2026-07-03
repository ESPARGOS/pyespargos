import QtQuick
import Custom
import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import "../common" as Common

Common.ESPARGOSApplication {
    id: mainWindow
    minimumWidth: 1600
    minimumHeight: 800
    visible: true
    color: "#11191e"
    title: " MIMO Radar Demo"

    appDrawerComponent: Component {
        Common.AppDrawer {
            id: appDrawer
            title: "Settings"
            endpoint: appconfig

            Label { Layout.columnSpan: 2; text: "Display Settings"; color: "#9fb3c8" }

            Label { text: "Oversampling"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: oversamplingSpinBox
                property string configKey: "oversampling"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 160
                from: 1
                to: 20
                value: 10
            }

            Button {
                id: startTransmissionButton
                text: "Start Radar"
                Layout.columnSpan: 2
                Layout.alignment: Qt.AlignCenter
                onClicked: backend.start_radar_slot()
            }

            Button {
                id: stopTransmissionButton
                text: "Stop Radar"
                Layout.columnSpan: 2
                Layout.alignment: Qt.AlignCenter
                onClicked: backend.stop_radar_slot()
            }

            Button {
                id: calculateTxDelayButton
                text: "Calculate TX and Delay Correction"
                Layout.columnSpan: 2
                Layout.alignment: Qt.AlignCenter
                onClicked: backend.calculate_tx_and_delay_correction_slot()
            }

            Label { Layout.columnSpan: 2; text: "Gain Override"; color: "#9fb3c8" }
            Label { text: "Override"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            Switch {
                id: gainOverrideSwitch
                property string configKey: "gain_override.gain_override_active"
                property string configProp: "checked"
                Component.onCompleted: appDrawer.configManager.register(this)
                onCheckedChanged: appDrawer.configManager.onControlChanged(this)
            }

            Label { text: "RX Boards Gain"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: rxGainSpinBox
                property string configKey: "gain_override.rx_gain"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 160
                enabled: gainOverrideSwitch.checked
                from: 0
                to: 76
                value: 25
            }

            Label { text: "RX Boards FFT"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: rxFFTSpinBox
                property string configKey: "gain_override.rx_fft"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 160
                enabled: gainOverrideSwitch.checked
                from: -128
                to: 127
                value: -15
            }

            Label { text: "TX Boards Gain"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: txGainSpinBox
                property string configKey: "gain_override.tx_gain"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 160
                enabled: gainOverrideSwitch.checked
                from: 0
                to: 76
                value: 25
            }

            Label { text: "TX Boards FFT"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: txFFTSpinBox
                property string configKey: "gain_override.tx_fft"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 160
                enabled: gainOverrideSwitch.checked
                from: -128
                to: 127
                value: -15
            }

            Label { text: "TX power"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            ComboBox {
                id: txPowerBox
                property string configKey: "tx_power"
                property string configProp: "currentValue"
                Component.onCompleted: appDrawer.configManager.register(this)
                onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 210
                model: ListModel {
                    ListElement { text: "2 dBm"; value: 8 }
                    ListElement { text: "5 dBm"; value: 20 }
                    ListElement { text: "7 dBm"; value: 28 }
                    ListElement { text: "8.5 dBm"; value: 34 }
                    ListElement { text: "11 dBm"; value: 44 }
                    ListElement { text: "13 dBm"; value: 52 }
                    ListElement { text: "14 dBm"; value: 56 }
                    ListElement { text: "15 dBm"; value: 60 }
                    ListElement { text: "16.5 dBm"; value: 66 }
                    ListElement { text: "18 dBm"; value: 72 }
                    ListElement { text: "20 dBm"; value: 80 }
                }
                textRole: "text"
                valueRole: "value"
                currentValue: 60
            }

            Label { text: "CSI Completion"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            ComboBox {
                id: csiCompletionBox
                property string configKey: "pred_completion"
                property string configProp: "currentValue"
                Component.onCompleted: appDrawer.configManager.register(this)
                onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 210
                model: ListModel {
                    ListElement { text: "TX + RX Antennas"; value: 0 }
                    ListElement { text: "TX Antennas"; value: 1 }
                    ListElement { text: "RX Antennas"; value: 2 }
                    ListElement { text: "All Antennas"; value: 3 }
                }
                textRole: "text"
                valueRole: "value"
            }

            Button {
                id: clearDataButton
                text: "Clear Data"
                Layout.columnSpan: 2
                Layout.alignment: Qt.AlignCenter
                onClicked: backend.clear_data_slot()
            }
        }
    }

    Rectangle {
        id: plotContainer

        property real leftMargin: 70
        property real bottomMargin: 50
        property real topMargin: 20
        property real rightMargin: 70
        property real axisTitleMargin: 48
        property real tickFontSize: 18

        property real plotW: plotH
        property real plotH: Math.min(parent.height, parent.width) - 210


        property var txTicks: [
            { pos: -1,   label: "π" },
            { pos: -0.5, label: "π/2" },
            { pos: 0,    label: "0" },
            { pos: 0.5,  label: "-π/2" },
            { pos: 1,    label: "-π" }
        ]
        
        property var rxTicks: [
            { pos: -1,   label: "-π" },
            { pos: -0.5, label: "-π/2" },
            { pos: 0,    label: "0" },
            { pos: 0.5,  label: "π/2" },
            { pos: 1,    label: "π" }
        ]

        width: plotW + leftMargin + rightMargin
        height: plotH + topMargin + bottomMargin
        anchors.centerIn: parent
        color: "transparent"

        Rectangle {
            id: plotArea
            x: plotContainer.leftMargin
            y: plotContainer.topMargin
            width: plotContainer.plotW
            height: plotContainer.plotH
            color: "#11191e"
            clip: true

            QImageTexture {
                id: textureSource
                visible: true
            }

            ShaderEffect {
                id: shader
                anchors.fill: parent
                property variant source: ShaderEffectSource {
                    sourceItem: textureSource
                    hideSource: true
                    smooth: false
                }
            }

            Canvas {
                id: gridCanvas
                anchors.fill: parent
                onWidthChanged: requestPaint()
                onHeightChanged: requestPaint()
                onPaint: {
                    var ctx = getContext("2d");
                    ctx.clearRect(0, 0, width, height);
                    ctx.strokeStyle = Qt.rgba(1, 1, 1, 0.25);
                    ctx.lineWidth = 1;

                    for (var x = -1; x <= 1; x += 0.5) {
                        var xFrac = (x + 1) * 0.5;
                        var xPos = xFrac * width;
                        ctx.beginPath();
                        ctx.moveTo(xPos, 0);
                        ctx.lineTo(xPos, height);
                        ctx.stroke();
                    }

                    for (var y = -1; y <= 1; y += 0.5) {
                        var yFrac = (y + 1) * 0.5;
                        var yPos = yFrac * height;
                        ctx.beginPath();
                        ctx.moveTo(0, yPos);
                        ctx.lineTo(width, yPos);
                        ctx.stroke();
                    }
                }
            }
        }

        // X-axis (TX beamspace) tick labels
        Repeater {
            model: plotContainer.txTicks
            delegate: Label {
                required property var modelData
                text: modelData.label
                color: "#aaaaaa"
                font.pixelSize: plotContainer.tickFontSize
                x: plotContainer.leftMargin + (modelData.pos + 1) * 0.5 * plotContainer.plotW - width / 2
                y: plotContainer.topMargin + plotContainer.plotH + 8
            }
        }

        // Y-axis (RX beamspace) tick labels, -pi at the bottom
        Repeater {
            model: plotContainer.rxTicks
            delegate: Label {
                required property var modelData
                text: modelData.label
                color: "#aaaaaa"
                font.pixelSize: plotContainer.tickFontSize
                horizontalAlignment: Text.AlignRight
                x: plotContainer.leftMargin - width - 8
                y: plotContainer.topMargin + (1 - (modelData.pos + 1) * 0.5) * plotContainer.plotH - height / 2
            }
        }

        // X-axis title
        Label {
            x: plotContainer.leftMargin + plotContainer.plotW / 2 - width / 2
            y: plotContainer.topMargin + plotContainer.plotH + plotContainer.axisTitleMargin
            text: "TX Beamspace in radians"
            color: "#aaaaaa"
            font.pixelSize: 20
            font.bold: true
        }

        // Y-axis title (rotated)
        Label {
            x: plotContainer.leftMargin - plotContainer.axisTitleMargin - height
            y: plotContainer.topMargin + plotContainer.plotH / 2 + width / 2
            text: "RX Beamspace in radians"
            color: "#aaaaaa"
            font.pixelSize: 20
            font.bold: true
            rotation: -90
            transformOrigin: Item.TopLeft
        }
    }

    Column {
        id: modeRow
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: 20
        anchors.leftMargin: 20
        spacing: 24

        Label { id: numpackets_0; text: `Frames/s TX 0: ${backend.packets_per_second[0]}`; color: "#ffffff"; font.pixelSize: 14 }
        Label { id: numpackets_1; text: `Frames/s TX 1: ${backend.packets_per_second[1]}`; color: "#ffffff"; font.pixelSize: 14 }
        Label { id: numpackets_2; text: `Frames/s TX 2: ${backend.packets_per_second[2]}`; color: "#ffffff"; font.pixelSize: 14 }
        Label { id: numpackets_3; text: `Frames/s TX 3: ${backend.packets_per_second[3]}`; color: "#ffffff"; font.pixelSize: 14 }
        Label { id: numpackets_4; text: `Frames/s TX 4: ${backend.packets_per_second[4]}`; color: "#ffffff"; font.pixelSize: 14 }
        Label { id: numpackets_5; text: `Frames/s TX 5: ${backend.packets_per_second[5]}`; color: "#ffffff"; font.pixelSize: 14 }
        Label { id: numpackets_6; text: `Frames/s TX 6: ${backend.packets_per_second[6]}`; color: "#ffffff"; font.pixelSize: 14 }
        Label { id: numpackets_7; text: `Frames/s TX 7: ${backend.packets_per_second[7]}`; color: "#ffffff"; font.pixelSize: 14 }
    }

    Button {
        id: clutterButton
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.topMargin: 20
        text: "Calculate Clutter"
        onClicked: backend.calculate_clutter_slot()
    }

    Timer {
        interval: 33
        running: true
        repeat: true
        onTriggered: {
            backend.update_data()
        }
    }

    Timer {
        interval: 1000
        running: true
        repeat: true
        onTriggered: {
            backend.update_status()
        }
    }

    Connections {
        target: backend
        function onDataChanged() {
            textureSource.update_texture()
        }
    }
}