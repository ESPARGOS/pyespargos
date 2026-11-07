import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import "../../common" as Common
import "../common" as BLE

Common.ESPARGOSApplication {
    id: window
    title: "Instantaneous BLE CSI: Frequency Domain"
    minimumWidth: 800
    minimumHeight: 500
    color: "#11191e"
    property var colors: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]
    receiverDrawerComponent: Component {
        BLE.BLERxDrawer {
            receiverConfig: bleController.receiverConfig
            busy: bleController.busy || backend.initializing
            calibrating: bleController.calibrating
            needsCalibration: bleController.needsCalibration
            calibrationSupported: bleController.calibrationSupported
            statusMessage: bleController.status
            onConfigurationRequested: settings => bleController.configureReceiver(settings)
            onCalibrationRequested: bleController.calibrate()
        }
    }
    appDrawerComponent: Component {
        Common.AppDrawer {
            id: settings
            title: "Settings"
            endpoint: appconfig
            Label { text: "Calibration" }
            Switch {
                property string configKey: "calibrated"
                property string configProp: "checked"
                Component.onCompleted: settings.configManager.register(this)
                onCheckedChanged: settings.configManager.onControlChanged(this)
            }
            Label { text: "Correct capture timing" }
            Switch {
                property string configKey: "correct_timing"
                property string configProp: "checked"
                Component.onCompleted: settings.configManager.register(this)
                onCheckedChanged: settings.configManager.onControlChanged(this)
            }
        }
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        Repeater {
            model: ["Power [dB]", "Phase [rad]"]
            delegate: ChartView {
                id: chart
                required property int index
                required property string modelData
                property var curves: []
                Layout.fillWidth: true
                Layout.fillHeight: true
                antialiasing: true
                backgroundColor: "#151f26"
                legend.visible: false
                property alias valueAxis: value
                ValueAxis {
                    id: frequency
                    min: bleController.receiverConfig.phy === "LE2M" ? -1 : -0.5
                    max: -min
                    titleText: "<font color=\"#e0e0e0\">Frequency offset [MHz]</font>"
                    titleFont.bold: false
                    labelsColor: "#e0e0e0"
                    gridLineColor: "#c0c0c0"
                    tickCount: 5
                    labelFormat: "%.2f"
                }
                ValueAxis {
                    id: value
                    min: chart.index === 0 ? -20 : -Math.PI
                    max: chart.index === 0 ? 20 : Math.PI
                    titleText: "<font color=\"#e0e0e0\">" + chart.modelData + "</font>"
                    titleFont.bold: false
                    labelsColor: "#e0e0e0"
                    gridLineColor: "#c0c0c0"
                    tickType: ValueAxis.TicksDynamic
                    tickInterval: chart.index === 0 ? 5 : 2
                }
                Component.onCompleted: {
                    for (let i = 0; i < 8; ++i) {
                        let series = createSeries(ChartView.SeriesTypeLine, "RX " + (i + 1), frequency, value)
                        series.color = window.colors[i]
                        series.useOpenGL = Qt.platform.os === "linux"
                        curves.push(series)
                    }
                    plots[index] = chart
                }
            }
        }
    }
    BLE.AdvertiserOverlay {
        advertiserModel: advertisers
        onSelectionRequested: (identity, selected) => {
            if (selected) advertisers.selectOnly(identity)
            else advertisers.select(identity, false)
        }
    }
    property var plots: []
    Timer {
        interval: 50
        repeat: true
        running: true
        onTriggered: {
            if (window.plots.length === 2)
                backend.updateCurves(window.plots[0].curves, window.plots[1].curves, window.plots[0].valueAxis)
        }
    }
}
