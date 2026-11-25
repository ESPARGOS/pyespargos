import QtQuick
import QtQuick.Controls
import "../../common" as Common

Common.BacklogWindowSettings {
    id: root
    property bool showFormatControls: true
    sizeKey: "size"
    ageKey: ""
    maximumPackets: 128
    packetConfigManager: backlogSettings

    Common.ConfigManager {
        id: backlogSettings
        endpoint: backlogconfig
        Component.onCompleted: fetchAndApply()
    }

    component BacklogSwitch: Switch {
        required property string configKey
        property string configProp: "checked"
        Component.onCompleted: if (root.showFormatControls || configKey === "filters.exclude_11b") backlogSettings.register(this)
        onToggled: backlogSettings.onControlChanged(this)
    }

    Label { text: "Store L-LTF"; visible: root.showFormatControls }
    BacklogSwitch { configKey: "fields.lltf"; visible: root.showFormatControls }
    Label { text: "Store HT20"; visible: root.showFormatControls }
    BacklogSwitch { configKey: "fields.ht20"; visible: root.showFormatControls }
    Label { text: "Store HT40"; visible: root.showFormatControls }
    BacklogSwitch { configKey: "fields.ht40"; visible: root.showFormatControls }
    Label { text: "Store HE20"; visible: root.showFormatControls }
    BacklogSwitch { configKey: "fields.he20"; visible: root.showFormatControls }
    Label { text: "Exclude 11b" }
    BacklogSwitch { configKey: "filters.exclude_11b" }
}
