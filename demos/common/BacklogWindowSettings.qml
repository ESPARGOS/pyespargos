import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

GridLayout {
    id: root
    required property var configManager
    property var packetConfigManager: configManager
    property string sizeKey: "backlog.size"
    property string ageKey: "backlog.max_age"
    property string antennaKey: ""
    property int minimumPackets: 1
    property int maximumPackets: 64
    property int minimumAntennas: 1
    property int maximumAntennas: 8
    property alias minimumAgeMs: age.from
    property alias maximumAgeMs: age.to
    property alias ageStepMs: age.stepSize

    columns: 2
    columnSpacing: 16
    rowSpacing: 8
    Layout.columnSpan: 2
    Layout.fillWidth: true

    Label { text: "Packet backlog"; color: "#9fb3c8"; Layout.columnSpan: 2 }
    Label { text: "Packets" }
    GainSlider {
        Layout.preferredWidth: 180
        from: root.minimumPackets; to: root.maximumPackets; stepSize: 1
        property string configKey: root.sizeKey
        property string configProp: "value"
        Component.onCompleted: root.packetConfigManager.register(this)
        onEdited: root.packetConfigManager.onControlChanged(this)
    }
    Label { text: "Max age"; visible: age.visible }
    GainSlider {
        id: age
        visible: root.ageKey !== ""
        Layout.preferredWidth: 180
        from: 50; to: 1000; stepSize: 25
        valueText: value === 0 ? "Off" : Math.round(value) + " ms"
        property string configKey: root.ageKey
        property string configProp: "value"
        property var encode: function(v) { return v / 1000 }
        property var decode: function(v) { return v * 1000 }
        Component.onCompleted: if (configKey) root.configManager.register(this)
        onEdited: root.configManager.onControlChanged(this)
    }
    Label { text: "Min. antennas"; visible: antennas.visible }
    SpinBox {
        id: antennas
        visible: root.antennaKey !== ""
        from: root.minimumAntennas; to: root.maximumAntennas
        property string configKey: root.antennaKey
        property string configProp: "value"
        Component.onCompleted: if (configKey) root.configManager.register(this)
        onValueModified: root.configManager.onControlChanged(this)
    }
}
