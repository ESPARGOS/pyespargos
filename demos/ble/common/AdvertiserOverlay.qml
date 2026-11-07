import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: advertiserPanel
    required property var advertiserModel
    signal selectionRequested(string identity, bool selected)
    objectName: "advertiserOverlay"
    anchors.top: parent.top
    anchors.right: parent.right
    anchors.margins: 16
    width: 340
    height: Math.min(260, parent.height * 0.35, advertiserHeader.height + Math.max(36, list.contentHeight) + 16)
    color: "#c0182229"
    border.color: "#405360"

    RowLayout {
        id: advertiserHeader
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: 8
        height: 28
        spacing: 8
        Label { text: "MAC"; Layout.fillWidth: true; leftPadding: 30; color: "#b6c6cf" }
        Label { text: "Packets"; Layout.preferredWidth: 62; horizontalAlignment: Text.AlignRight; color: "#b6c6cf" }
        Label { text: "Last (s)"; Layout.preferredWidth: 60; horizontalAlignment: Text.AlignRight; color: "#b6c6cf" }
    }
    ListView {
        id: list
        objectName: "advertiserList"
        anchors.top: advertiserHeader.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 8
        anchors.topMargin: 0
        clip: true
        model: advertiserPanel.advertiserModel
        ScrollBar.vertical: ScrollBar {}
        delegate: ItemDelegate {
            id: advertiser
            required property string identity
            required property string address
            required property int packets
            required property real age
            required property bool selected
            width: list.width
            height: 36
            onClicked: advertiserPanel.selectionRequested(identity, !selected)
            background: Rectangle { color: advertiser.selected ? "#60408050" : advertiser.hovered ? "#30485865" : "transparent" }
            contentItem: RowLayout {
                spacing: 8
                CheckBox {
                    checked: advertiser.selected
                    Layout.preferredWidth: 24
                    Layout.preferredHeight: 28
                    onToggled: advertiserPanel.selectionRequested(advertiser.identity, checked)
                }
                Label { text: advertiser.address; font.family: "monospace"; font.pixelSize: 12; Layout.fillWidth: true }
                Label { text: advertiser.packets; Layout.preferredWidth: 62; horizontalAlignment: Text.AlignRight }
                Label { text: advertiser.age.toFixed(1); Layout.preferredWidth: 60; horizontalAlignment: Text.AlignRight }
            }
        }
        Label { anchors.centerIn: parent; visible: list.count === 0; text: "Waiting for advertisers…"; color: "#b6c6cf" }
    }
}
