pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../common" as Common
import "../common" as IQ
import "../../common/camera" as Camera

Common.ESPARGOSApplication {
    id: window
    title: "IQ Camera"
    visible: true
    minimumWidth: 800
    minimumHeight: 520

    receiverDrawerComponent: Component {
        IQ.IQRxDrawer {
            controller: iqcontrol
            allowSignal: !controller || controller.boardCount === 1
            thresholdControl: backend
            triggerDefaults: backend.triggerDefaults
            compact: true
        }
    }

    appDrawerComponent: Component {
        Common.AppDrawer {
            id: drawer
            title: "IQ Camera"
            endpoint: appconfig
            implicitWidth: 400

            Label { text: "Beamforming"; color: "#9fb3c8"; Layout.columnSpan: 2 }
            Label { text: "Method"; color: "white" }
            ComboBox {
                Layout.fillWidth: true
                property string configKey: "beamformer.type"
                property string configProp: "currentValue"
                textRole: "text"
                valueRole: "value"
                model: [{text: "MUSIC", value: "MUSIC"}, {text: "Bartlett", value: "Bartlett"}]
                Component.onCompleted: drawer.configManager.register(this)
                onActivated: drawer.configManager.onControlChanged(this)
            }

            Label { text: "Detection threshold"; color: "white" }
            Camera.ValueSlider {
                Layout.fillWidth: true
                property string configKey: "beamformer.threshold_db"
                property string configProp: "value"
                Component.onCompleted: drawer.configManager.register(this)
                onEdited: drawer.configManager.onControlChanged(this)
                from: 3; to: 30; stepSize: 1
                valueText: Math.round(value) + " dB"
                ToolTip.visible: hovered
                ToolTip.text: "Minimum signal power above the receiver noise floor"
            }

            Label { text: "Direction labels"; color: "white" }
            Switch {
                property string configKey: "visualization.markers"
                property string configProp: "checked"
                Component.onCompleted: drawer.configManager.register(this)
                onToggled: drawer.configManager.onControlChanged(this)
            }
            Label { text: "Waterfall"; color: "white" }
            Switch {
                property string configKey: "visualization.waterfall"
                property string configProp: "checked"
                Component.onCompleted: drawer.configManager.register(this)
                onToggled: drawer.configManager.onControlChanged(this)
            }

            Camera.CameraSettings { configManager: drawer.configManager }
        }
    }

    Camera.CameraOverlay {
        updateInterval: 25
        anchors.fill: parent
    }

    Label {
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.topMargin: 10
        visible: overlayModel.captureStatus !== "Calibrated"
        text: overlayModel.captureStatus
        color: "white"
        padding: 6
        background: Rectangle { color: "#d0101418"; radius: 4 }
    }

    IQ.IQWaterfall {
        visible: overlayModel.waterfallVisible
        timeDescription: backend.waterfallTimeDescription
        model: overlayModel
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 14
        width: Math.min(500, parent.width * 0.42)
        height: Math.min(230, parent.height * 0.30)
    }
}
