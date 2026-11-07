import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

// Firmware values stay independent of button order and supported routes.
RowLayout {
    id: root
    property int value: 2
    property var modes: [0, 2, 3, 1]
    signal activated(int value)
    spacing: 0

    function isUserActive() {
        for (let i = 0; i < buttons.count; ++i)
            if (buttons.itemAt(i).down) return true
        return false
    }

    Repeater {
        id: buttons
        model: root.modes
        delegate: Button {
            required property int modelData
            required property int index
            objectName: "rfSwitch" + modelData
            Layout.preferredWidth: 48
            Layout.minimumWidth: 44
            Layout.preferredHeight: 48
            topInset: 0
            bottomInset: 0
            leftPadding: 8
            rightPadding: 8
            Material.background: checked ? Material.accent : "#34434b"
            Material.foreground: "white"
            Material.roundedScale: Material.NotRounded
            Material.elevation: 0
            checkable: true
            autoExclusive: true
            checked: root.value === modelData
            readonly property string description: modelData === 0 ? "Isolated"
                : modelData === 1 ? "Reference network"
                : modelData === 2 ? "Antennas, first polarization"
                : modelData === 3 ? "Antennas, second polarization" : "Random polarization"
            Accessible.name: description
            ToolTip.visible: hovered
            ToolTip.text: description
            onClicked: {
                root.value = modelData
                root.activated(modelData)
            }
            text: modelData === 0 ? "Iso" : modelData === 1 ? "Ref" : ""
            icon.source: modelData < 2 ? "" : modelData === 4 ? "img/shuffle.svg"
                : modelData === 3 ? "img/polarization-second.svg" : "img/polarization.svg"
            icon.color: "white"
            icon.width: 24
            icon.height: 24
            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                anchors.topMargin: 8
                anchors.bottomMargin: 8
                width: 1
                color: "#607580"
                visible: index > 0
            }
        }
    }
}
