import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick
import QtCharts
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 1000
	minimumHeight: 650

	color: "#11191e"
	title: "Radar CSI"

	// Tab20 color cycle reordered: https://github.com/matplotlib/matplotlib/blob/main/lib/matplotlib/_cm.py#L1293
	property var colorCycle: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5", "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5"]
	property var amplitudeSeries: []
	property var phaseSeries: []

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Radar CSI Settings"
			endpoint: appconfig

			Label { Layout.columnSpan: 2; text: "Radar Schedule"; color: "#9fb3c8" }

			Label { text: "Period [ms]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			RowLayout {
				spacing: 10
				Slider {
					id: periodSlider
					property string configKey: "period_ms"
					property string configProp: "value"
					property var encode: function(v) { return Math.round(v * 10) / 10 }
					property var decode: function(v) { return Math.max(7, Math.min(100, Number(v === undefined || v === null || v === "" ? 12 : v))) }
					Component.onCompleted: appDrawer.configManager.register(this)
					onValueChanged: appDrawer.configManager.onControlChanged(this)
					from: 7
					to: 100
					value: 16
					stepSize: 1
					implicitWidth: 145
					function isUserActive() { return pressed }
				}
				Label { text: periodSlider.value.toFixed(0); color: "#ffffff"; font.family: "monospace"; Layout.preferredWidth: 56; horizontalAlignment: Text.AlignRight }
			}

			Label { text: "Start [ms]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			RowLayout {
				spacing: 10
				Slider {
					id: startSlider
					property string configKey: "start_ms"
					property string configProp: "value"
					property var encode: function(v) { return Math.round(v * 10) / 10 }
					property var decode: function(v) { return Math.max(0, Math.min(200, Number(v === undefined || v === null || v === "" ? 10 : v))) }
					Component.onCompleted: appDrawer.configManager.register(this)
					onValueChanged: appDrawer.configManager.onControlChanged(this)
					from: 0
					to: 200
					value: 10
					stepSize: 0.1
					implicitWidth: 145
					function isUserActive() { return pressed }
				}
				Label { text: startSlider.value.toFixed(1); color: "#ffffff"; font.family: "monospace"; Layout.preferredWidth: 56; horizontalAlignment: Text.AlignRight }
			}

			Label { text: "Slot [ms]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			RowLayout {
				spacing: 10
				Slider {
					id: slotSlider
					property string configKey: "slot_ms"
					property string configProp: "value"
					property var encode: function(v) { return Math.round(v * 10) / 10 }
					property var decode: function(v) { return Math.max(0.1, Math.min(100, Number(v === undefined || v === null || v === "" ? 10 : v))) }
					Component.onCompleted: appDrawer.configManager.register(this)
					onValueChanged: appDrawer.configManager.onControlChanged(this)
					from: 0.1
					to: 100
					value: 10
					stepSize: 0.1
					implicitWidth: 145
					function isUserActive() { return pressed }
				}
				Label { text: slotSlider.value.toFixed(1); color: "#ffffff"; font.family: "monospace"; Layout.preferredWidth: 56; horizontalAlignment: Text.AlignRight }
			}

			Label { text: "TX Offset [ns]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			RowLayout {
				spacing: 10
				Slider {
					id: txOffsetSlider
					property string configKey: "tx_timestamp_offset_ns"
					property string configProp: "value"
					property var encode: function(v) { return Math.round(v) }
					property var decode: function(v) { return Math.max(-5000, Math.min(5000, parseInt(v === undefined || v === null || v === "" ? 1063 : v))) }
					Component.onCompleted: appDrawer.configManager.register(this)
					onValueChanged: appDrawer.configManager.onControlChanged(this)
					from: 1060
					to: 1100
					value: 1085
					stepSize: 0.1
					implicitWidth: 145
					function isUserActive() { return pressed }
				}
				Label { text: Math.round(txOffsetSlider.value); color: "#ffffff"; font.family: "monospace"; Layout.preferredWidth: 56; horizontalAlignment: Text.AlignRight }
			}

			Label { text: "Enable Radar"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: enableRadarSwitch
				checked: true
				onToggled: {
					if (checked)
						backend.applyRadarSchedule()
					else
						backend.disableRadarSchedule()
				}
			}

		}
	}

	ColumnLayout {
		anchors.fill: parent

		ChartView {
			id: csiAmplitude
			Layout.fillWidth: true
			Layout.fillHeight: true
			legend.visible: false
			antialiasing: true
			animationOptions: ChartView.NoAnimation
			backgroundColor: "#151f26"

			axes: [
				ValueAxis {
					id: csiAmplitudeSubcarrierAxis
					min: -Math.floor(backend.subcarrierCount / 2)
					max: Math.floor(backend.subcarrierCount / 2) - 1
					titleText: "<font color=\"#e0e0e0\">Subcarrier Index</font>"
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickInterval: 20
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#e0e0e0"
					labelFormat: "%d"
				},
				ValueAxis {
					id: csiAmplitudeAxis
					min: -70
					max: 70
					titleText: "<font color=\"#e0e0e0\">Power [dB]</font>"
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickInterval: 10
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#e0e0e0"
				}
			]

			Component.onCompleted: {
				amplitudeSeries = []
				for (let link = 0; link < backend.linkCount; ++link) {
					let series = csiAmplitude.createSeries(ChartView.SeriesTypeLine, backend.linkName(link), csiAmplitudeSubcarrierAxis, csiAmplitudeAxis)
					series.pointsVisible = false
					series.color = colorCycle[link % colorCycle.length]
					series.useOpenGL = Qt.platform.os === "linux"
					amplitudeSeries.push(series)
				}
			}
		}

		ChartView {
			id: csiPhase
			Layout.fillWidth: true
			Layout.fillHeight: true
			legend.visible: false
			antialiasing: true
			animationOptions: ChartView.NoAnimation
			backgroundColor: "#151f26"

			axes: [
				ValueAxis {
					id: csiPhaseSubcarrierAxis
					min: -Math.floor(backend.subcarrierCount / 2)
					max: Math.floor(backend.subcarrierCount / 2) - 1
					titleText: "<font color=\"#e0e0e0\">Subcarrier Index</font>"
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickInterval: 20
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#e0e0e0"
					labelFormat: "%d"
				},
				ValueAxis {
					id: csiPhaseAxis
					min: -3.14
					max: 3.14
					titleText: "<font color=\"#e0e0e0\">Phase [rad]</font>"
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickInterval: 2
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#e0e0e0"
				}
			]

			Component.onCompleted: {
				phaseSeries = []
				for (let link = 0; link < backend.linkCount; ++link) {
					let series = csiPhase.createSeries(ChartView.SeriesTypeLine, backend.linkName(link), csiPhaseSubcarrierAxis, csiPhaseAxis)
					series.pointsVisible = false
					series.color = colorCycle[link % colorCycle.length]
					series.useOpenGL = Qt.platform.os === "linux"
					phaseSeries.push(series)
				}
			}
		}
	}

	Timer {
		id: updateTimer
		interval: 50
		running: !backend.initializing
		repeat: true
		onTriggered: backend.updateCSI(amplitudeSeries, phaseSeries, csiAmplitudeAxis)
	}
}
