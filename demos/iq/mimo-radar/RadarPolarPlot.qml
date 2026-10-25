import QtQuick
import QtQuick.Controls

Rectangle {
    id: plot
    color: "#192329"
    radius: 6
    property real rangeMaximum: 15
    property real floorDb: 12
    property real spanDb: 12
    property bool linearScale: true

    Label {
        anchors.top: parent.top
        anchors.topMargin: 18
        anchors.horizontalCenter: parent.horizontalCenter
        text: "Range–azimuth"
        font.pixelSize: 22
    }
    Image {
        id: samples
        visible: false
        cache: false
        smooth: true
        source: "image://radar/azimuth/" + backend.revision
    }
    Item {
        anchors.fill: parent
        anchors.margins: 44
        anchors.topMargin: 90
        anchors.bottomMargin: 80
        Item {
            id: chart
            width: Math.min(parent.width, parent.height * 2)
            height: width / 2
            anchors.centerIn: parent
            ShaderEffect {
                anchors.fill: parent
                property var source: samples
                property real floorDb: plot.floorDb
                property real spanDb: plot.spanDb
                property real linearScale: plot.linearScale ? 1 : 0
                property real xLow: 0
                property real xHigh: 1
                property real legend: 0
                property real beamspacePeriod: backend.beamspacePeriod
                property real polar: 1
                vertexShader: "heatmap.vert.qsb"
                fragmentShader: "heatmap.frag.qsb"
            }
            Canvas {
                anchors.fill: parent
                onWidthChanged: requestPaint()
                onHeightChanged: requestPaint()
                onPaint: {
                    const ctx = getContext("2d");
                    ctx.reset();
                    ctx.strokeStyle = "#40ffffff";
                    ctx.lineWidth = 1;
                    for (let ring = 1; ring <= 3; ++ring) {
                        ctx.beginPath();
                        ctx.arc(width / 2, height, height * ring / 3, Math.PI, 2 * Math.PI);
                        ctx.stroke();
                    }
                    for (let angle = -90; angle <= 90; angle += 30) {
                        const a = angle * Math.PI / 180;
                        ctx.beginPath();
                        ctx.moveTo(width / 2, height);
                        ctx.lineTo(width / 2 + height * Math.sin(a), height - height * Math.cos(a));
                        ctx.stroke();
                    }
                }
            }
            Repeater {
                model: 7
                Label {
                    required property int index
                    readonly property real angle: (index * 30 - 90) * Math.PI / 180
                    x: chart.width / 2 + (chart.height + 22) * Math.sin(angle) - width / 2
                    y: chart.height - (chart.height + 22) * Math.cos(angle) - height / 2
                    text: (index * 30 - 90) + "°"
                    font.pixelSize: 15
                }
            }
            Repeater {
                model: 3
                Label {
                    required property int index
                    x: chart.width / 2 + chart.height * (index + 1) / 3 - width / 2
                    y: chart.height + 14
                    text: ((index + 1) * plot.rangeMaximum / 3).toFixed(0) + " m"
                    font.pixelSize: 15
                }
            }
        }
    }
}
