import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../../common" as Common

// Reusable controls with no camera or transport dependency. Only user edits
// request configuration. Controller snapshots never send settings back.
Common.AppDrawer {
    id: root
    property var receiverConfig: ({channel: 37, gain: 40, polarization: 2, phy: "LE1M", gain_mode: "auto", decimation: 0})
    property bool busy: false
    property bool calibrating: false
    property bool needsCalibration: true
    property bool calibrationSupported: true
    property string statusMessage: ""
    readonly property int rfSwitchMode: polarization.value
    signal configurationRequested(var settings)
    signal calibrationRequested()
    objectName: "bleReceiverDrawer"
    title: "Receiver Settings"
    edge: Qt.LeftEdge
    implicitWidth: 400

    function loadConfig() {
        if (!channel || !gain || !polarization || !phy || !automatic || !decimation || !samples) return
        channel.currentIndex = channel.indexOfValue(receiverConfig.channel)
        gain.value = receiverConfig.gain
        polarization.value = receiverConfig.polarization
        phy.currentIndex = phy.indexOfValue(receiverConfig.phy)
        automatic.checked = receiverConfig.gain_mode === "auto"
        decimation.currentIndex = decimation.indexOfValue(receiverConfig.decimation)
    }
    function configure() {
        if (busy) return
        configurationRequested({channel: channel.currentValue, gain: Math.round(gain.value),
            polarization: polarization.value, phy: phy.currentValue,
            gain_mode: automatic.checked ? "auto" : "manual",
            decimation: decimation.currentValue})
    }
    onReceiverConfigChanged: loadConfig()
    Component.onCompleted: loadConfig()

    Label { Layout.columnSpan: 2; text: "Channel / PHY"; color: "#9fb3c8" }
    Label { text: "Channel"; Layout.alignment: Qt.AlignRight }
    ComboBox {
        id: channel
        enabled: !root.busy
        implicitWidth: 220
        textRole: "text"
        valueRole: "value"
        model: [37, 38, 39].concat(Array.from({length: 37}, (_, i) => i)).map(c => ({value: c,
            text: c + " (" + (c === 37 ? 2402 : c === 38 ? 2426 : c === 39 ? 2480 : 2404 + 2*c + (c >= 11 ? 2 : 0)) + " MHz)"}))
        onActivated: root.configure()
    }
    Label { text: "PHY"; Layout.alignment: Qt.AlignRight }
    ComboBox {
        id: phy
        objectName: "blePhy"
        enabled: !root.busy
        implicitWidth: 220
        textRole: "text"
        valueRole: "value"
        model: [{text: "LE 1M / Coded", value: "LE1M"}, {text: "LE 2M", value: "LE2M"}]
        onActivated: root.configure()
    }

    Label { Layout.columnSpan: 2; text: "Calibration"; color: "#9fb3c8" }
    Button {
        objectName: "bleCalibrate"
        text: root.calibrating ? "Calibrating…" : "Trigger Calibration"
        Layout.columnSpan: 2
        Layout.alignment: Qt.AlignHCenter
        enabled: !root.busy && root.calibrationSupported
        Material.background: root.needsCalibration && root.calibrationSupported ? "#7f1d1d" : root.Material.background
        Material.foreground: "white"
        onClicked: root.calibrationRequested()
    }
    ProgressBar {
        Layout.columnSpan: 2
        Layout.fillWidth: true
        visible: root.calibrating
        indeterminate: true
    }
    Label {
        Layout.columnSpan: 2
        Layout.maximumWidth: 340
        Layout.fillWidth: true
        text: root.statusMessage
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        visible: text.length > 0
        color: "#b6c6cf"
    }

    Label { Layout.columnSpan: 2; text: "Signal Path"; color: "#9fb3c8" }
    Label { text: "RF input"; Layout.alignment: Qt.AlignRight }
    Common.RFSwitchControl {
        id: polarization
        modes: [0, 2, 3, 4, 1]
        enabled: !root.busy
        onActivated: root.configure()
    }
    Label { Layout.columnSpan: 2; text: "Gain"; color: "#9fb3c8" }
    Label { text: "Automatic"; Layout.alignment: Qt.AlignRight }
    Switch { id: automatic; enabled: !root.busy; onToggled: root.configure() }
    Label { text: "RX Gain"; Layout.alignment: Qt.AlignRight }
    Common.GainSlider {
        id: gain
        objectName: "bleGainSlider"
        enabled: !root.busy && !automatic.checked
        toolTipText: "Manual receiver gain. Reduce it if the IQ samples clip."
        implicitWidth: 220
        onEdited: root.configure()
    }

    Label { Layout.columnSpan: 2; text: "Capture"; color: "#9fb3c8" }
    Label { text: "Sample rate"; Layout.alignment: Qt.AlignRight }
    ComboBox {
        id: decimation
        enabled: !root.busy
        implicitWidth: 220
        textRole: "text"
        valueRole: "value"
        model: [{text: "Auto", value: 0}, {text: "4 MSa/s", value: 8}, {text: "8 MSa/s", value: 4},
                {text: "16 MSa/s", value: 2}]
        onActivated: root.configure()
    }
}
