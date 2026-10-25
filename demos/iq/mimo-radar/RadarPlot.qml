import QtQuick
import QtQuick.Controls

Rectangle {
    id: plot
    color: "#192329"
    radius: 6
    property string title
    property string textureName
    property string axisLabel
    property real minimum: -70
    property real maximum: 70
    property real dataMinimum: minimum
    property real dataMaximum: maximum
    property real rangeMaximum: 15
    property real floorDb: 12
    property real spanDb: 12
    property bool linearScale: true

    Label {
        anchors.top: parent.top
        anchors.topMargin: 18
        anchors.horizontalCenter: parent.horizontalCenter
        text: plot.title
        font.pixelSize: 22
    }
    Image {
        id: samples
        visible: false
        cache: false
        smooth: true
        source: "image://radar/" + plot.textureName + "/" + backend.revision
    }
    Item {
        id: chart
        anchors.fill: parent
        anchors.margins: 24
        anchors.leftMargin: 76
        anchors.topMargin: 64
        anchors.bottomMargin: 74
        ShaderEffect {
            objectName: "heatmapShader"
            anchors.fill: parent
            property var source: samples
            property real floorDb: plot.floorDb
            property real spanDb: plot.spanDb
            property real linearScale: plot.linearScale ? 1 : 0
            property real xLow: (plot.minimum - plot.dataMinimum) / (plot.dataMaximum - plot.dataMinimum)
            property real xHigh: (plot.maximum - plot.dataMinimum) / (plot.dataMaximum - plot.dataMinimum)
            property real beamspacePeriod: backend.beamspacePeriod
            property real polar: 0
            property real legend: 0
            vertexShader: "heatmap.vert.qsb"
            fragmentShader: "heatmap.frag.qsb"
        }
        Repeater {
            model: 5
            Item {
                required property int index
                x: index * chart.width / 4
                height: chart.height
                Rectangle { width: 1; height: parent.height; color: "#24ffffff" }
                Label {
                    anchors.top: parent.bottom
                    anchors.topMargin: 10
                    anchors.horizontalCenter: parent.left
                    text: (plot.minimum + index * (plot.maximum - plot.minimum) / 4).toFixed(plot.textureName === "doppler" ? 1 : 0)
                    font.pixelSize: 15
                }
            }
        }
        Repeater {
            model: 4
            Item {
                required property int index
                y: (1 - index / 3) * chart.height
                width: chart.width
                Rectangle { height: 1; width: parent.width; color: "#24ffffff" }
                Label {
                    anchors.right: parent.left
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.top
                    text: (index * plot.rangeMaximum / 3).toFixed(0)
                    font.pixelSize: 15
                }
            }
        }
    }
    Label {
        anchors.horizontalCenter: chart.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 16
        text: plot.axisLabel
        font.pixelSize: 16
    }
    Label {
        x: 14 - width / 2
        y: chart.y + (chart.height - height) / 2
        rotation: -90
        text: "Range relative to zero (m)"
        font.pixelSize: 16
    }
}
