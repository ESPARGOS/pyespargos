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

    Common.ConfigManager {
        id: settings
        endpoint: appconfig
        Component.onCompleted: fetchAndApply()
    }
    QtObject {
        property string configKey: "floor_db"
        property string configProp: "value"
        property alias value: window.floorDb
        Component.onCompleted: settings.register(this)
        onValueChanged: settings.onControlChanged(this)
    }
    QtObject {
        property string configKey: "ceiling_db"
        property string configProp: "value"
        property alias value: window.ceilingDb
        Component.onCompleted: settings.register(this)
        onValueChanged: settings.onControlChanged(this)
    }
    QtObject {
        property string configKey: "linear_scale"
        property string configProp: "value"
        property alias value: window.linearScale
        Component.onCompleted: settings.register(this)
        onValueChanged: settings.onControlChanged(this)
    }
    QtObject {
        property string configKey: "automatic_maximum"
        property string configProp: "value"
        property alias value: window.automaticMaximum
        Component.onCompleted: settings.register(this)
        onValueChanged: settings.onControlChanged(this)
    }
    QtObject {
        property string configKey: "full_velocity_range"
        property string configProp: "value"
        property alias value: window.fullVelocityRange
        Component.onCompleted: settings.register(this)
        onValueChanged: settings.onControlChanged(this)
    }

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
            Label { text: "TX calibration" }
            ComboBox {
                objectName: "txCalibrationSelector"
                Layout.fillWidth: true
                model: ["Automatic", "Monitor", "Reciprocity"]
                enabled: !backend.busy
                property string configKey: "tx_calibration"
                property string configProp: "currentIndex"
                function decode(value) { return ["auto", "monitor", "reciprocity"].indexOf(value) }
                function encode(value) { return ["auto", "monitor", "reciprocity"][value] }
                Component.onCompleted: settings.register(this)
                onActivated: settings.onControlChanged(this)
            }
            Label { text: "Frequency (MHz)" }
            TextField {
                objectName: "frequencyInput"
                Layout.fillWidth: true
                enabled: !backend.busy
                selectByMouse: true
                validator: DoubleValidator { bottom: 1e-06; top: 1000000000.0; decimals: 6; locale: "C"; notation: DoubleValidator.StandardNotation }
                property string configKey: "frequency"
                property string configProp: "text"
                function decode(value) { return String(value) }
                function encode(value) { return Number(value) }
                Component.onCompleted: settings.register(this)
                onEditingFinished: {
                    if (acceptableInput) settings.onControlChanged(this)
                }
            }
            Label { text: "TX amplitude" }
            TextField {
                objectName: "amplitudeInput"
                Layout.fillWidth: true
                enabled: !backend.busy
                selectByMouse: true
                validator: DoubleValidator { bottom: 1e-06; top: 1; decimals: 6; locale: "C"; notation: DoubleValidator.StandardNotation }
                property string configKey: "amplitude"
                property string configProp: "text"
                function decode(value) { return String(value) }
                function encode(value) { return Number(value) }
                Component.onCompleted: settings.register(this)
                onEditingFinished: {
                    if (acceptableInput) settings.onControlChanged(this)
                }
            }
            Label { text: "Integration time (s)" }
            TextField {
                objectName: "integration_timeInput"
                Layout.fillWidth: true
                enabled: !backend.busy
                selectByMouse: true
                validator: DoubleValidator { bottom: 0.1; top: 2; decimals: 6; locale: "C"; notation: DoubleValidator.StandardNotation }
                property string configKey: "integration_time"
                property string configProp: "text"
                function decode(value) { return String(value) }
                function encode(value) { return Number(value) }
                Component.onCompleted: settings.register(this)
                onEditingFinished: {
                    if (acceptableInput) settings.onControlChanged(this)
                }
            }
            Label { text: "RX gain" }
            TextField {
                objectName: "rx_gainInput"
                Layout.fillWidth: true
                enabled: !backend.busy
                selectByMouse: true
                validator: DoubleValidator { bottom: 0; top: 76; decimals: 0; locale: "C"; notation: DoubleValidator.StandardNotation }
                property string configKey: "rx_gain"
                property string configProp: "text"
                function decode(value) { return String(value) }
                function encode(value) { return Number(value) }
                Component.onCompleted: settings.register(this)
                onEditingFinished: {
                    if (acceptableInput) settings.onControlChanged(this)
                }
            }
            Label { text: "RX sample rate" }
            ComboBox {
                objectName: "sampleRateSelector"
                Layout.fillWidth: true
                model: backend.sampleRates
                currentIndex: backend.sampleRateIndex
                enabled: !backend.busy
                property string configKey: "sample_rate"
                property string configProp: "currentIndex"
                function decode(value) { return backend.sampleRateValues.indexOf(value) }
                function encode(value) { return backend.sampleRateValues[value] }
                Component.onCompleted: settings.register(this)
                onActivated: settings.onControlChanged(this)
            }
            Label { text: "TX antenna feed" }
            ComboBox {
                objectName: "txFeedSelector"
                Layout.fillWidth: true
                model: ["Left", "Right"]
                currentIndex: backend.txFeed
                enabled: !backend.busy
                property string configKey: "tx_feed"
                property string configProp: "currentIndex"
                function decode(value) { return value }
                function encode(value) { return value }
                Component.onCompleted: settings.register(this)
                onActivated: settings.onControlChanged(this)
            }
            Label { text: "RX antenna feed" }
            ComboBox {
                objectName: "rxFeedSelector"
                Layout.fillWidth: true
                model: ["Left", "Right"]
                currentIndex: backend.rxFeed
                enabled: !backend.busy
                property string configKey: "rx_feed"
                property string configProp: "currentIndex"
                function decode(value) { return value }
                function encode(value) { return value }
                Component.onCompleted: settings.register(this)
                onActivated: settings.onControlChanged(this)
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
                currentIndex: window.fullVelocityRange ? 1 : 0
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
