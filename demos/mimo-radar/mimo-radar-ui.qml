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
    title: " MIMO Radar Demo"

    appDrawerComponent: Component {
        Common.AppDrawer {
            id:appDrawer
            title: "Settings"
            endpoint:appconfig

            Label { Layout.columnSpan: 2; text: "Display Settings"; color: "#9fb3c8" }

            Label { text: "Delay Min"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: delayMinSpinBox
                property string configKey: "delay_min"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 160
                from: -10
                to: 19
                value: -3
            }

            Label { text: "Delay Max"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: delayMaxSpinBox
                property string configKey: "delay_max"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 160
                from: -9
                to: 20
                value: 5
            }

            Button {
                id:startTransmissionButton
                text: "Start Radar"
                Layout.columnSpan: 2;
                Layout.alignment: Qt.AlignCenter
                onClicked: backend.start_radar_slot()
            }
            
            Button {
                id:stopTransmissionButton
                text: "Stop Radar"
                Layout.columnSpan: 2;
                Layout.alignment: Qt.AlignCenter
                onClicked: backend.stop_radar_slot()
            }

            Button {
                id:calculateTxDelayButton
                text: "Calculate TX and Delay Correction"
				Layout.columnSpan: 2;
                Layout.alignment: Qt.AlignCenter
                onClicked: backend.calculate_tx_and_delay_correction_slot()
            }

            Button {
                id: clearDataButton
                text: "Clear Data"
				Layout.columnSpan: 2;
                Layout.alignment: Qt.AlignCenter
                onClicked: backend.clear_data_slot()
            }

        }
    }

Rectangle{
    id: plotContainer

    property real leftMargin: 70
    property real bottomMargin: 50
    property real topMargin: 20
    property real rightMargin: 70

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
                smooth: true
            }
            vertexShader: "vertex_shader.qsb"
            fragmentShader: "fragment_shader.qsb"
        }
    }

    Canvas {
    id: canvas
    anchors.fill: parent
    property real origin_x: plotContainer.leftMargin + plotContainer.plotW * 0.5
    property real origin_y: plotContainer.topMargin + plotContainer.plotH
    property var phi_num: 6
    property var text_color: "white"
    property var text_width: 1
    property var text_font: "14px sans-serif"
    property var delay_min: backend.delay_min
    property var delay_max: backend.delay_max
    property var num_delay: delay_max - delay_min
    property color lineColor: Qt.rgba(0.3, 0.3, 0.3, 0.9)
    property real lineWidth: 2.5

    onPaint: {
        var context = getContext("2d");
        context.clearRect(0, 0, canvas.width, canvas.height);
        context.font = text_font;
        context.fillStyle = text_color;
        context.lineWidth = text_width;
        context.textAlign = "right";
        context.textBaseline = "top";
        context.fillText(delay_min, origin_x, origin_y + 5);
        for (let i = 0; i < num_delay; i++) {
            context.beginPath();
            context.strokeStyle = lineColor;
            context.lineWidth = lineWidth;
            context.arc(origin_x, origin_y, (1 - (1 / num_delay) * i) * plotContainer.plotH, Math.PI, 2 * Math.PI);
            context.stroke();
            context.fillStyle = text_color;
            context.lineWidth = text_width;
            context.textAlign = "right";
            context.textBaseline = "top";
            context.fillText(((delay_max - i)), origin_x + (1 - (1 / num_delay) * i) * plotContainer.plotH, origin_y + 5);
        }
        for (let i = -phi_num / 2; i <= phi_num / 2; i++) {
            context.beginPath();
            context.strokeStyle = lineColor;
            context.lineWidth = lineWidth;
            context.moveTo(origin_x, origin_y);
            context.lineTo(origin_x + Math.sin(i / phi_num * Math.PI) * plotContainer.plotH, origin_y - Math.cos(i / phi_num * Math.PI) * plotContainer.plotH);
            context.stroke();
            if (i < 0) {
                context.fillStyle = text_color;
                context.lineWidth = text_width;
                context.textAlign = "right";
                context.textBaseline = "bottom";
                context.fillText(180 / phi_num * (-i) + "°", origin_x + Math.sin(i / phi_num * Math.PI) * (plotContainer.plotH + 5), origin_y - Math.cos(i / phi_num * Math.PI) * (plotContainer.plotH + 5));
            } else if (i == 0) {
                context.fillStyle = text_color;
                context.lineWidth = text_width;
                context.textAlign = "center";
                context.textBaseline = "bottom";
                context.fillText(0 + "°", origin_x + Math.sin(i / phi_num * Math.PI) * (plotContainer.plotH + 5), origin_y - Math.cos(i / phi_num * Math.PI) * (plotContainer.plotH + 5));
            } else {
                context.fillStyle = text_color;
                context.lineWidth = text_width;
                context.textAlign = "left";
                context.textBaseline = "bottom";
                context.fillText(180 / phi_num * (-i) + "°", origin_x + Math.sin(i / phi_num * Math.PI) * (plotContainer.plotH + 5), origin_y - Math.cos(i / phi_num * Math.PI) * (plotContainer.plotH + 5));
            }
        }
    }

    Connections {
        target: backend
        function onConfigChanged() {
            canvas.requestPaint();
        }
    }
}

}

    Button {
        id: clutterButton
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.topMargin: 20
        text: "Calculate Clutter"
        onClicked: backend.calculate_clutter_slot()
    }

    Timer{
        interval: 16
        running: true
        repeat: true
        onTriggered: {
        backend.update_data()
        }
    }
    Connections {
        target: backend
        function onDataChanged(){
            textureSource.update_texture()
        }
    }
}