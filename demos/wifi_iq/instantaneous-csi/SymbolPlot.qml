import QtQuick
import QtCharts

Item {
    ChartView {
        id: chart
        anchors.centerIn: parent
        width: Math.min(parent.width, parent.height)
        height: width
        backgroundColor: "#151f26"
        legend.alignment: Qt.AlignBottom
        legend.labelColor: "#e0e0e0"
        antialiasing: true

        component SymbolAxis: ValueAxis {
            min: -1.8
            max: 1.8
            tickCount: 7
            labelFormat: "%.1f"
            labelsColor: "#e0e0e0"
            gridLineColor: "#52606a"
        }
        SymbolAxis { id: iAxis; titleText: "<font color='#e0e0e0'>I</font>" }
        SymbolAxis { id: qAxis; titleText: "<font color='#e0e0e0'>Q</font>" }
        component Points: ScatterSeries { axisX: iAxis; axisY: qAxis; markerSize: 5; borderColor: color }
        Points { id: training; name: "Preamble"; color: "#56b4e9" }
        Points { id: signal; name: "L-SIG"; color: "#e69f00" }
        Points { id: htSignal; name: "HT-SIG"; color: "#cc79a7" }
        Points { id: pilots; name: "Pilots"; color: "#eee176" }
        Points { id: data; name: "Data"; color: "#009e73" }
        Timer {
            interval: 80
            running: chart.visible && !backend.initializing
            repeat: true
            onTriggered: backend.updateSymbols([training, signal, htSignal, pilots, data])
        }
    }
}
