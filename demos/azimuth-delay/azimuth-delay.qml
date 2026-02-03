import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Shapes
import QtQuick

import "../common" as Common

Common.ESPARGOSApplication {
    id: window
    title: "Azimuth-Delay Demo"
    minimumWidth: 1024
    minimumHeight: 768

    appDrawerComponent: Component {
        Common.AppDrawer {
            id: appDrawer
            title: "Settings"
            endpoint: appconfig

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

            Common.GenericAppSettings {
                id: genericAppSettings
                insertBefore: genericAppSettingsAnchor
            }

            Item {
                id: genericAppSettingsAnchor
                Layout.columnSpan: 2
                width: 0
                height: 0
                visible: false
            }

            Common.BacklogSettings {
                id: backlogSettings
                insertBefore: backlogSettingsAnchor
            }

            Item {
                id: backlogSettingsAnchor
                Layout.columnSpan: 2
                width: 0
                height: 0
                visible: false
            }

            // Spacer
            Rectangle {
                id: endSpacer
                Layout.columnSpan: 2
                width: 1; height: 30
                color: "transparent"
            }
        }
    }

    Rectangle {
        id: plotArea
        height: 0.8 * Math.min(parent.height, parent.width * 0.4)
        width: 2 * height
        anchors.centerIn: parent
        color: "#11191e"
        property color lineColor: Qt.rgba(0.3, 0.3, 0.3, 0.9)
        property real lineWidth: 2.5

        ShaderEffect {
            id: shader
            anchors.fill: parent
            Canvas {
                id: plotCanvas
                width: backend.angleSize
                height: backend.delaySize
                property var imageData: undefined
                function createImageData() {
                    const ctx = plotCanvas.getContext("2d");
                    imageData = ctx.createImageData(width, height);
                }

                onAvailableChanged: if (available) createImageData();

                onPaint: {
                    if (plotCanvas.imageData) {
                        const ctx = plotCanvas.getContext("2d");
                        ctx.drawImage(plotCanvas.imageData, 0, 0);
                    }
                }
            }
            property variant source: ShaderEffectSource {
                sourceItem: plotCanvas
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
        property var origin_x: 0.5 * canvas.width
        property var origin_y: 0.5 * (canvas.height - plotArea.height) + plotArea.height
        property var phi_num: 6
        property var text_color: "white"
        property var text_width: 1
        property var text_font: "14px sans-serif"
        property var delay_min: backend.delayMin
        property var delay_max: backend.delayMax
        property var num_delay: delay_max - delay_min

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
                context.strokeStyle = plotArea.lineColor;
                context.lineWidth = plotArea.lineWidth;
                context.arc(origin_x, origin_y, (1 - (1 / num_delay) * i) * plotArea.height, Math.PI, 2 * Math.PI);
                context.stroke();
                context.fillStyle = text_color;
                context.lineWidth = text_width;
                context.textAlign = "right";
                context.textBaseline = "top";
                context.fillText(((delay_max - i)), origin_x + (1 - (1 / num_delay) * i) * plotArea.height, origin_y + 5);
            }
            for (let i = -phi_num / 2; i <= phi_num / 2; i++) {
                context.beginPath();
                context.strokeStyle = plotArea.lineColor;
                context.lineWidth = plotArea.lineWidth;
                context.moveTo(origin_x, origin_y);
                context.lineTo(origin_x + Math.sin(i / phi_num * Math.PI) * plotArea.height, origin_y - Math.cos(i / phi_num * Math.PI) * plotArea.height);
                context.stroke();
                if (i < 0) {
                    context.fillStyle = text_color;
                    context.lineWidth = text_width;
                    context.textAlign = "right";
                    context.textBaseline = "bottom";
                    context.fillText(180 / phi_num * (-i) + "°", origin_x + Math.sin(i / phi_num * Math.PI) * (plotArea.height + 5), origin_y - Math.cos(i / phi_num * Math.PI) * (plotArea.height + 5));
                } else if (i == 0) {
                    context.fillStyle = text_color;
                    context.lineWidth = text_width;
                    context.textAlign = "center";
                    context.textBaseline = "bottom";
                    context.fillText(0 + "°", origin_x + Math.sin(i / phi_num * Math.PI) * (plotArea.height + 5), origin_y - Math.cos(i / phi_num * Math.PI) * (plotArea.height + 5));
                } else {
                    context.fillStyle = text_color;
                    context.lineWidth = text_width;
                    context.textAlign = "left";
                    context.textBaseline = "bottom";
                    context.fillText(180 / phi_num * (-i) + "°", origin_x + Math.sin(i / phi_num * Math.PI) * (plotArea.height + 5), origin_y - Math.cos(i / phi_num * Math.PI) * (plotArea.height + 5));
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

    Timer {
        interval: 1000 / 60
        running: !backend.initializing
        repeat: true
        onTriggered: {
            backend.update_data();
        }
    }

    Connections {
        target: backend
        function onDataChanged(image_data) {
            if (plotCanvas.imageData === undefined || image_data.length !== plotCanvas.imageData.data.length) {
                const ctx = plotCanvas.getContext("2d");
                plotCanvas.imageData = ctx.createImageData(backend.angleSize, backend.delaySize);
            }
            
            let len = plotCanvas.imageData.data.length;
            for (let i = 0; i < len; i++) {
                plotCanvas.imageData.data[i] = image_data[i];
            }
            plotCanvas.requestPaint();
        }
    }
}

