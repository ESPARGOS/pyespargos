import QtQuick

Item {
    id: root
    property int rowCount: 2
    property int columns: 4
    property var panels: [{}]
    readonly property real tileSize: Math.max(1, Math.min(width / (columns + 2),
        (height - arrays.spacing * (panels.length - 1)) / (rowCount * panels.length + 1)))
    Column {
        id: arrays
        anchors.centerIn: parent
        spacing: 24
        Repeater {
            model: root.panels
            PhaseArray {
                required property var modelData
                rowCount: root.rowCount
                columns: root.columns
                tileSize: root.tileSize
                colors: modelData.colors || []
                labels: modelData.labels || []
            }
        }
    }
}
