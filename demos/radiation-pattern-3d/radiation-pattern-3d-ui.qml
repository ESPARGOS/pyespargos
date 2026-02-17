import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick3D
import QtQuick3D.AssetUtils
import "../common" as Common

Common.ESPARGOSApplication {
    id: window
    visible: true
    minimumWidth: 1024
    minimumHeight: 768

    color: "#11191e"
    title: "3D Radiation Pattern"

    appDrawerComponent: Component {
        Common.AppDrawer {
            id: appDrawer
            title: "Settings"
            endpoint: appconfig

            // --- Pattern Settings ---
            Label { Layout.columnSpan: 2; text: "Pattern Settings"; color: "#9fb3c8" }

            Label { text: "Color Mode"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            ComboBox {
                id: colorModeCombo
                property string configKey: "color_mode"
                property string configProp: "currentValue"
                Component.onCompleted: appDrawer.configManager.register(this)
                onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 180
                model: [
                    { value: "power", text: "Power" },
                    { value: "delay", text: "Delay" }
                ]
                textRole: "text"
                valueRole: "value"
                currentValue: "power"
            }

            Label { text: "Max Delay"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: colorModeCombo.currentValue === "delay" }
            RowLayout {
                spacing: 8
                visible: colorModeCombo.currentValue === "delay"
                Slider {
                    id: maxDelaySlider
                    property string configKey: "max_delay"
                    property string configProp: "value"
                    property var encode: function(v) { return Math.round(v * 100) / 100 }
                    property var decode: function(v) { return Math.max(0.01, Math.min(0.8, parseFloat(v || 0.2))) }
                    Component.onCompleted: appDrawer.configManager.register(this)
                    onValueChanged: appDrawer.configManager.onControlChanged(this)
                    from: 0.01; to: 0.8; value: 0.2; stepSize: 0.01
                    implicitWidth: 120
                    function isUserActive() { return pressed }
                    ToolTip.visible: hovered
                    ToolTip.text: "In samples. Color hue indicates relative delay up to this maximum. Current value: " + value.toFixed(2)
                }
                Label { text: maxDelaySlider.value.toFixed(2); color: "#ffffff"; Layout.preferredWidth: 40 }
            }

            Label { text: "Scale"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            RowLayout {
                spacing: 8
                Slider {
                    id: patternScaleSlider
                    property string configKey: "pattern_scale"
                    property string configProp: "value"
                    property var encode: function(v) { return Math.round(v) }
                    property var decode: function(v) { return Math.max(10, Math.min(300, parseInt(v || 100))) }
                    Component.onCompleted: appDrawer.configManager.register(this)
                    onValueChanged: appDrawer.configManager.onControlChanged(this)
                    from: 10; to: 300; value: 100; stepSize: 5
                    implicitWidth: 120
                    function isUserActive() { return pressed }
                }
                Label { text: patternScaleSlider.value; color: "#ffffff"; Layout.preferredWidth: 30 }
            }

            Label { text: "Element Pattern"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            Switch {
                id: elementPatternSwitch
                property string configKey: "element_pattern"
                property string configProp: "checked"
                property var encode: function(v) { return v }
                property var decode: function(v) { return !!v }
                Component.onCompleted: appDrawer.configManager.register(this)
                onCheckedChanged: appDrawer.configManager.onControlChanged(this)
                checked: true
            }

            Label { text: "Polarization"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            ComboBox {
                id: polarizationCombo
                property string configKey: "polarization_mode"
                property string configProp: "currentValue"
                Component.onCompleted: appDrawer.configManager.register(this)
                onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 180
                model: [
                    { value: "ignore", text: "Ignore" },
                    { value: "incorporate", text: "Incorporate" }
                ]
                textRole: "text"
                valueRole: "value"
                currentValue: "ignore"
            }

            // --- View Settings ---
            Label { Layout.columnSpan: 2; text: "View Settings"; color: "#9fb3c8" }

            Label { text: "Show Array"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            Switch {
                id: showArraySwitch
                checked: true
                onCheckedChanged: arrayGroup.visible = checked
            }

            Label { text: "Show Axes"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            Switch {
                id: showAxesSwitch
                checked: false
                onCheckedChanged: axesGroup.visible = checked
            }

            Button {
                Layout.columnSpan: 2
                Layout.alignment: Qt.AlignCenter
                text: "Reset Camera"
                onClicked: {
                    cameraYaw = -20
                    cameraPitch = 25
                    cameraDistance = 300
                }
            }

            Common.GenericAppSettings {
                id: genericAppSettings
                insertBefore: genericAppSettingsAnchor
                implicitWidth: 180
            }

            Item {
                id: genericAppSettingsAnchor
                Layout.columnSpan: 2
                width: 0; height: 0; visible: false
            }

            Common.BacklogSettings {
                id: backlogSettings
                insertBefore: backlogSettingsAnchor
            }

            Item {
                id: backlogSettingsAnchor
                Layout.columnSpan: 2
                width: 0; height: 0; visible: false
            }

            Rectangle {
                id: endSpacer
                Layout.columnSpan: 2
                width: 1; height: 30
                color: "transparent"
            }
        }
    }

    // --- Camera orbit state ---
    property real cameraYaw: -20
    property real cameraPitch: 25
    property real cameraDistance: 300

    // --- 3D View ---
    View3D {
        id: view3d
        anchors.fill: parent

        environment: SceneEnvironment {
            clearColor: "#11191e"
            backgroundMode: SceneEnvironment.Color
            antialiasingMode: SceneEnvironment.MSAA
            antialiasingQuality: SceneEnvironment.High
        }

        // Camera on an orbit around the origin
        PerspectiveCamera {
            id: camera
            fieldOfView: 60
            clipNear: 1
            clipFar: 2000

            position: {
                var yawRad = cameraYaw * Math.PI / 180
                var pitchRad = cameraPitch * Math.PI / 180
                var x = cameraDistance * Math.cos(pitchRad) * Math.sin(yawRad)
                var y = cameraDistance * Math.sin(pitchRad)
                var z = cameraDistance * Math.cos(pitchRad) * Math.cos(yawRad)
                return Qt.vector3d(x, y, z)
            }

            eulerRotation: {
                return Qt.vector3d(-cameraPitch, cameraYaw, 0)
            }
        }

        // Lighting
        DirectionalLight {
            eulerRotation: Qt.vector3d(-30, -30, 0)
            brightness: 0.8
            ambientColor: Qt.rgba(0.3, 0.3, 0.3, 1.0)
        }

        DirectionalLight {
            eulerRotation: Qt.vector3d(30, 150, 0)
            brightness: 0.4
        }

        // Array board 3D models
        Node {
            id: arrayGroup
            visible: true

            Repeater3D {
                model: backend.boardPlacements
                Node {
                    // Board placement in combined array
                    position: Qt.vector3d(modelData.x, modelData.y, 0)
                    eulerRotation: Qt.vector3d(0, 0, modelData.z_rot)
                    RuntimeLoader {
                        source: backend.arrayModelSource
                        // Shift from corner origin to board center
                        position: Qt.vector3d(backend.arrayModelOriginOffset.y, backend.arrayModelOriginOffset.x, 0)
                        // Rotate to face boresight (Z axis)
                        eulerRotation: Qt.vector3d(-90, 0, 0)
                        scale: Qt.vector3d(backend.arrayModelScale, backend.arrayModelScale, backend.arrayModelScale)
                    }
                }
            }
        }

        // Coordinate axes helper
        Node {
            id: axesGroup
            visible: false

            // X axis (red)
            Model {
                source: "#Cylinder"
                scale: Qt.vector3d(0.005, 0.6, 0.005)
                position: Qt.vector3d(30, 0, 0)
                eulerRotation: Qt.vector3d(0, 0, -90)
                materials: DefaultMaterial { diffuseColor: "#ff3333" }
            }
            // Y axis (green)
            Model {
                source: "#Cylinder"
                scale: Qt.vector3d(0.005, 0.6, 0.005)
                position: Qt.vector3d(0, 30, 0)
                materials: DefaultMaterial { diffuseColor: "#33ff33" }
            }
            // Z axis (blue)
            Model {
                source: "#Cylinder"
                scale: Qt.vector3d(0.005, 0.6, 0.005)
                position: Qt.vector3d(0, 0, 30)
                eulerRotation: Qt.vector3d(90, 0, 0)
                materials: DefaultMaterial { diffuseColor: "#3377ff" }
            }
        }

        // Radiation pattern mesh
        Model {
            id: patternModel
            geometry: patternGeometry
            materials: [
                PrincipledMaterial {
                    lighting: PrincipledMaterial.NoLighting
                    vertexColorsEnabled: true
                    alphaMode: PrincipledMaterial.Opaque
                    cullMode: PrincipledMaterial.NoCulling
                }
            ]
        }
    }

    // Mouse / touch interaction for orbit camera
    MouseArea {
        id: dragArea
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton

        property real lastX: 0
        property real lastY: 0

        onPressed: (mouse) => {
            lastX = mouse.x
            lastY = mouse.y
        }

        onPositionChanged: (mouse) => {
            var dx = mouse.x - lastX
            var dy = mouse.y - lastY
            lastX = mouse.x
            lastY = mouse.y

            if (mouse.buttons & Qt.LeftButton) {
                cameraYaw += dx * 0.3
                cameraPitch = Math.max(-89, Math.min(89, cameraPitch + dy * 0.3))
            }
        }

        onWheel: (wheel) => {
            var delta = wheel.angleDelta.y / 120
            cameraDistance = Math.max(50, Math.min(800, cameraDistance - delta * 20))
        }
    }

    // Touch / pinch support for zoom
    PinchArea {
        anchors.fill: parent
        property real startDistance: 0

        onPinchStarted: {
            startDistance = cameraDistance
        }

        onPinchUpdated: (pinch) => {
            cameraDistance = Math.max(50, Math.min(800, startDistance / pinch.scale))
        }
    }

    // Update timer
    Timer {
        interval: 1000 / 30
        running: !backend.initializing
        repeat: true
        onTriggered: backend.updateRequest()
    }
}
