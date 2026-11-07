import QtQuick

Grid {
    id: root
    property int rowCount: 2
    columns: 4
    property real tileSize: 100
    property var colors: []
    property var labels: []
    spacing: -2
    Repeater {
        model: root.rowCount * root.columns
        Rectangle {
            required property int index
            width: root.tileSize
            height: width
            color: root.colors.length > index ? Qt.rgba(...root.colors[index]) : "#34424c"
            border.width: 2
            border.color: "white"
            Text {
                anchors.centerIn: parent
                text: root.labels.length > parent.index ? root.labels[parent.index] : ""
                color: "white"
                style: Text.Outline
                styleColor: "#202830"
                horizontalAlignment: Text.AlignHCenter
            }
        }
    }
}
