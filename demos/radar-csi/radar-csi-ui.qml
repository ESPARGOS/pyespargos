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

			// Shared radar TX settings and RX data-format selector (used across radar demos).
			// The leftover component roots + anchor span both columns so they don't break the
			// grid's 2-column parity (matches the doppler demo).
			Common.RadarSettings { insertBefore: radarAnchor; Layout.columnSpan: 2 }
			Common.GenericAppSettings { insertBefore: radarAnchor; controlWidth: 150; Layout.columnSpan: 2 }
			Item { id: radarAnchor; width: 0; height: 0; Layout.columnSpan: 2 }
		}
	}

	ColumnLayout {
		anchors.fill: parent

		// Sits in its own row above the charts (which shift down), so it never overlaps the curves.
		Button {
			id: clearCurvesButton
			Layout.alignment: Qt.AlignHCenter
			Layout.topMargin: 20
			text: "Clear CSI Curves"
			onClicked: {
				backend.clearCSICurves()
				for (let i = 0; i < amplitudeSeries.length; ++i)
					amplitudeSeries[i].clear()
				for (let i = 0; i < phaseSeries.length; ++i)
					phaseSeries[i].clear()
			}
		}

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
