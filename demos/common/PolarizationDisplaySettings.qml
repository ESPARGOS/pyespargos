import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

GridLayout {
    id: root
    property var configManager
    columns: 2
    columnSpacing: 16
    Layout.columnSpan: 2
    Layout.fillWidth: true
    Label { text: "Grid spacing"; Layout.preferredWidth: 120 }
    GainSlider {
        Layout.preferredWidth: 180
        from: 8; to: 128; stepSize: 1
        valueText: Math.round(value) + " px"
        property string configKey: "beamformer.grid_spacing"
        property string configProp: "value"
        Component.onCompleted: root.configManager.register(this)
        onEdited: root.configManager.onControlChanged(this)
    }
    Label { text: "Phasor speed" }
    GainSlider {
        Layout.preferredWidth: 180
        from: 0; to: 8; stepSize: 0.25
        valueText: value === 0 ? "Paused" : value.toFixed(2) + " Hz"
        property string configKey: "beamformer.polarization_speed"
        property string configProp: "value"
        Component.onCompleted: root.configManager.register(this)
        onEdited: root.configManager.onControlChanged(this)
    }
}
