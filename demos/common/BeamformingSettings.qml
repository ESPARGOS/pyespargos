import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

GridLayout {
    id: root
    property var configManager
    property bool polarizationSupported: false
    property bool busy: false
    signal polarizationRequested(string mode)
    property string backlogTitle: "Measurement backlog"
    columns: 2
    columnSpacing: 16
    rowSpacing: 8
    Layout.columnSpan: 2
    Layout.fillWidth: true
    Label { text: "Beamforming"; color: "#9fb3c8"; Layout.columnSpan: 2 }
    Label { text: "Method"; Layout.preferredWidth: 120 }
    ComboBox {
        objectName: "beamformingMethod"
        Layout.preferredWidth: 180
        model: ["Bartlett", "MUSIC", "Root-MUSIC"]
        property string configKey: "beamformer.type"
        property string configProp: "currentIndex"
        property var encode: function(v) { return model[v] }
        property var decode: function(v) { return Math.max(0, model.indexOf(v)) }
        Component.onCompleted: root.configManager.register(this)
        onActivated: root.configManager.onControlChanged(this)
    }
    Label { text: "Multipath" }
    Switch {
        property string configKey: "beamformer.multipath"
        property string configProp: "checked"
        Component.onCompleted: root.configManager.register(this)
        onToggled: root.configManager.onControlChanged(this)
        ToolTip.visible: hovered
        ToolTip.text: "Spatial smoothing for MUSIC methods. Trades some resolution for multipath robustness."
    }
    Label { text: "Polarization"; visible: root.polarizationSupported }
    ComboBox {
        id: polarization
        enabled: !root.busy
        visible: root.polarizationSupported
        Layout.preferredWidth: 180
        model: ["Ignore", "Incorporate", "Show"]
        property var modes: ["ignore", "incorporate", "show"]
        property string configKey: "beamformer.polarization_mode"
        property string configProp: "currentIndex"
        property var encode: function(v) { return modes[v] }
        property var decode: function(v) { return Math.max(0, modes.indexOf(v)) }
        Component.onCompleted: if (root.polarizationSupported) root.configManager.register(this)
        onActivated: {
            root.configManager.onControlChanged(this)
            root.polarizationRequested(modes[currentIndex])
        }
    }
    PolarizationDisplaySettings {
        configManager: root.configManager
        visible: root.polarizationSupported && polarization.currentIndex === 2
    }
    Label { text: root.backlogTitle; color: "#9fb3c8"; Layout.columnSpan: 2 }
    Label { text: "Packets" }
    GainSlider {
        Layout.preferredWidth: 180
        from: 2; to: 64; stepSize: 1
        property string configKey: "beamformer.backlog_size"
        property string configProp: "value"
        Component.onCompleted: root.configManager.register(this)
        onEdited: root.configManager.onControlChanged(this)
    }
    Label { text: "Max age" }
    GainSlider {
        Layout.preferredWidth: 180
        from: 50; to: 1000; stepSize: 25
        valueText: Math.round(value) + " ms"
        property string configKey: "beamformer.backlog_age"
        property string configProp: "value"
        property var encode: function(v) { return v / 1000 }
        property var decode: function(v) { return v * 1000 }
        Component.onCompleted: root.configManager.register(this)
        onEdited: root.configManager.onControlChanged(this)
    }
}
