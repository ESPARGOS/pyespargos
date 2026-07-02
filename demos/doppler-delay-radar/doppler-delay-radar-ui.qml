import QtQuick
import Custom
import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import "../common" as Common



Common.ESPARGOSApplication {
    id:mainWindow
    minimumWidth: 1600
    minimumHeight: 800
    visible: true
    color: "#11191e"
    title: "Radar Demo"

    appDrawerComponent: Component {
        Common.AppDrawer {
            id: appDrawer
            title: "Settings"
            endpoint: appconfig

            // Shared radar TX settings and RX data-format selector (used across radar demos).
            // These reparent their controls into this grid; the leftover component roots + anchor
            // must each span both columns so they occupy empty full-width rows and don't shift the
            // 2-column parity of the controls that follow (RX Array, Oversampling).
            Common.RadarSettings { insertBefore: radarAnchor; Layout.columnSpan: 2 }
            Common.GenericAppSettings { insertBefore: radarAnchor; controlWidth: 150; Layout.columnSpan: 2 }
            Item { id: radarAnchor; width: 0; height: 0; Layout.columnSpan: 2 }

            // Receiver array selection (bistatic: process only the CSI of the chosen RX array)
            Label { text: "RX Array"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            ComboBox {
                id: rxArrayCombo
                property string configKey: "rx_array"
                property string configProp: "currentValue"
                Component.onCompleted: appDrawer.configManager.register(this)
                onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 150
                model: backend.rxArrays
                textRole: "text"
                valueRole: "value"
                currentValue: 0
            }

            // Doppler/delay processing settings (specific to this demo)
            Label { Layout.columnSpan: 2; text: "Oversampling"; color: '#ffffff' }
            Label { text: "Doppler"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true}
            SpinBox{
                id:dopplerOversampling
                property string configKey: "doppler_oversampling"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 150
                from: 1
                to: 21
                value: 3
                stepSize: 2
            }

            Label { text: "Delay"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true}
            SpinBox{
                id:delayOversampling
                property string configKey: "delay_oversampling"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 150
                from: 1
                to: 20
                value: 10
            }

            Button {
                id: clearDataButton
                text: "Clear Data"
                Layout.columnSpan: 2;
                Layout.alignment: Qt.AlignCenter
                onClicked: backend.clear_data()
            }
        }
    }


Rectangle{
    id: plotContainer

    property real leftMargin: 70
    property real bottomMargin: 50
    property real topMargin: 20
    property real rightMargin: 20

    property real plotW: 2 * plotH
    property real plotH: Math.min(parent.height, parent.width * 0.5) - 210

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
        color: "lightgray"
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
            vertexShader: "vertex_shader.qsb"
            fragmentShader: "fragment_shader.qsb"
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

                var delayMin = backend.delay_min;
                var delayMax = backend.delay_max;
                var dopplerRange = backend.doppler_range;
                var delaySpan = delayMax - delayMin;
                var dopplerSpan = 2 * dopplerRange;

                // Vertical grid lines (Doppler axis = X)
                var dopplerStep = Math.max(1, Math.round(dopplerRange / 5));
                for (var dop = -dopplerRange; dop <= dopplerRange; dop += dopplerStep) {
                    var xFrac = (dop + dopplerRange) / dopplerSpan;
                    var xPos = xFrac * width;
                    ctx.beginPath();
                    ctx.moveTo(xPos, 0);
                    ctx.lineTo(xPos, height);
                    ctx.stroke();
                }

                // Horizontal grid lines (Delay axis = Y)
                for (var d = delayMin; d <= delayMax; d++) {
                    var yFrac = 1 - ((d - delayMin) / delaySpan);
                    var yPos = yFrac * height;
                    ctx.beginPath();
                    ctx.moveTo(0, yPos);
                    ctx.lineTo(width, yPos);
                    ctx.stroke();
                }

                // Zero-Doppler line (center vertical)
                ctx.strokeStyle = Qt.rgba(1, 1, 1, 0.5);
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                ctx.moveTo(width / 2, 0);
                ctx.lineTo(width / 2, height);
                ctx.stroke();

                // Zero-Delay line (horizontal) if in range
                if (delayMin <= 0 && delayMax >= 0) {
                    var yZero = (1 - ((0 - delayMin) / delaySpan)) * height;
                    ctx.beginPath();
                    ctx.moveTo(0, yZero);
                    ctx.lineTo(width, yZero);
                    ctx.stroke();
                }
            }
        }
    }

    // X-axis tick labels (Doppler)
    Repeater {
        id: xTickRepeater
        property int dopplerStep: Math.max(1, Math.round(backend.doppler_range / 5))
        property int tickCount: Math.floor(2 * backend.doppler_range / dopplerStep) + 1
        model: tickCount
        Label {
            property int dopplerVal: -backend.doppler_range + index * xTickRepeater.dopplerStep
            property real dopplerSpeed: dopplerVal * backend.spacing_doppler
            property real xFrac: (dopplerVal + backend.doppler_range) / (2 * backend.doppler_range)
            x: plotContainer.leftMargin + xFrac * plotContainer.plotW - width / 2
            y: plotContainer.topMargin + plotContainer.plotH + 4
            text: dopplerSpeed.toFixed(1)
            color: "#ffffff"
            font.pixelSize: 12
            horizontalAlignment: Text.AlignHCenter
        }
    }

    // X-axis title
    Label {
        x: plotContainer.leftMargin + plotContainer.plotW / 2 - width / 2
        y: plotContainer.topMargin + plotContainer.plotH + 26
        text: "Doppler in m/s"
        color: "#aaaaaa"
        font.pixelSize: 13
        font.bold: true
    }

    // Y-axis tick labels (Delay)
    Repeater {
        model: backend.delay_max - backend.delay_min + 1
        Label {
            property real delayVal: (backend.delay_min + index)*backend.spacing_range
            property real yFrac: 1 - (index / (backend.delay_max - backend.delay_min))
            x: plotContainer.leftMargin - width - 6
            y: plotContainer.topMargin + yFrac * plotContainer.plotH - height / 2
            text: delayVal.toFixed(1)
            color: "#ffffff"
            font.pixelSize: 12
            horizontalAlignment: Text.AlignRight
        }
    }

    // Y-axis title (rotated)
    Label {
        x: 4
        y: plotContainer.topMargin + plotContainer.plotH / 2 + width / 2
        text: "Delay in m"
        color: "#aaaaaa"
        font.pixelSize: 13
        font.bold: true
        rotation: -90
        transformOrigin: Item.TopLeft
    }

    // Border around plot
    Rectangle {
        x: plotContainer.leftMargin
        y: plotContainer.topMargin
        width: plotContainer.plotW
        height: plotContainer.plotH
        color: "transparent"
        border.color: "#666666"
        border.width: 1.5
    }
}



    Column {
        id: modeRow
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.topMargin: 20
        anchors.leftMargin: 20
        spacing: 24


        Label{  id: numpackets
                text: `Packets/s: ${backend.packets_per_second} (expected: ${backend.num_tx_packets})`
                color: "#ffffff"
                font.pixelSize: 14
            }

        Label{  id: radarStatus
                text: `Status: ${backend.radar_status}`
                color: "#ffffff"
                font.pixelSize: 14
            }
    }

    Button {
        id: clutterButton
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.topMargin: 20
        text: "Calculate Clutter"
        onClicked: backend.calculate_clutter()
    }

    Timer{
        interval: 16
        running: true
        repeat: true
        onTriggered: {
        backend.update_data()
        }
    }

    Timer{
        interval: 1000
        running: true
        repeat: true
        onTriggered:{
        backend.update_status()
        }
    }
    Connections {
        target: backend
        function onDataChanged(){
            textureSource.update_texture()
        }
    }
}
