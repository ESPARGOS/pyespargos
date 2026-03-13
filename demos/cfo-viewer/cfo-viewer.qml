import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import QtQuick
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 900
	minimumHeight: 600

	color: "#11191e"
	title: "CFO Over Time"

	property var colorCycle: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5", "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5"]

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig

			Label { Layout.columnSpan: 2; text: "CFO Settings"; color: "#9fb3c8" }

			Label { text: "Max Age (s)"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: maxAgeSpinBox
				property string configKey: "max_age"
				property string configProp: "value"
				property var encode: function(v) { return v }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				from: 1
				to: 60
				value: 10
			}

			Common.GenericAppSettings {
				id: genericAppSettings
				insertBefore: genericAppSettingsAnchor
			}

			Item {
				id: genericAppSettingsAnchor
				Layout.columnSpan: 2
				width: 0
				height: 0
				visible: false
			}

			Common.BacklogSettings {
				id: backlogSettings
				insertBefore: backlogSettingsAnchor
			}

			Item {
				id: backlogSettingsAnchor
				Layout.columnSpan: 2
				width: 0
				height: 0
				visible: false
			}

			Rectangle {
				Layout.columnSpan: 2
				width: 1
				height: 30
				color: "transparent"
			}
		}
	}

	Rectangle {
		anchors.fill: parent
		anchors.margins: 10
		color: "#151f26"

		ChartView {
			id: cfoChart
			anchors.fill: parent
			anchors.margins: 10

			antialiasing: true
			backgroundColor: "#11191e"
			legend.visible: true
			legend.labelColor: "#e0e0e0"

			property var newDataBacklog: Array()

			ValueAxis {
				id: cfoXAxis
				min: 0
				max: 20
				titleText: "<font color=\"#e0e0e0\">Time in s</font>"
				titleFont.bold: false
				gridLineColor: "#c0c0c0"
				tickInterval: 5
				tickType: ValueAxis.TicksDynamic
				labelsColor: "#e0e0e0"
			}

			ValueAxis {
				id: cfoHzAxis
				min: -100000
				max: 100000
				titleText: "<font color=\"#e0e0e0\">CFO in Hz</font>"
				titleFont.bold: false
				gridLineColor: "#c0c0c0"
				tickInterval: 5000
				tickType: ValueAxis.TicksDynamic
				labelsColor: "#e0e0e0"
			}

			ValueAxis {
				id: cfoPpmYAxis
				min: cfoHzAxis.min * backend.cfoPpmScale
				max: cfoHzAxis.max * backend.cfoPpmScale
				titleText: "<font color=\"#e0e0e0\">CFO in ppm</font>"
				titleFont.bold: false
				gridVisible: true
				gridLineColor: "#98b9d8"
				tickInterval: 0.5
				tickType: ValueAxis.TicksDynamic
				labelFormat: "%.1f"
				labelsColor: "#98b9d8"
			}

			LineSeries {
				// Fake series to show the ppm axis.
				id: ppmAxisSeries
				visible: false
				axisX: cfoXAxis
				axisYRight: cfoPpmYAxis
			}

			function updatePpmAxis() {
				let scale = backend.cfoPpmScale
				cfoPpmYAxis.min = cfoHzAxis.min * scale
				cfoPpmYAxis.max = cfoHzAxis.max * scale

				ppmAxisSeries.clear()
				ppmAxisSeries.append(cfoXAxis.min, cfoPpmYAxis.min)
				ppmAxisSeries.append(cfoXAxis.max, cfoPpmYAxis.max)
			}

			Component.onCompleted: {
				let antennas = backend.sensorCount

				for (let ant = 0; ant < antennas; ++ant) {
					let series = cfoChart.createSeries(ChartView.SeriesTypeLine, "Ant " + ant, cfoXAxis, cfoHzAxis)
					series.pointsVisible = false
					series.color = colorCycle[ant % colorCycle.length]
					series.useOpenGL = Qt.platform.os === "linux"
				}

				updatePpmAxis()
			}

			Timer {
				interval: 1 / 40 * 1000
				running: true
				repeat: true
				onTriggered: {
					let minY = Infinity
					let maxY = -Infinity

					for (const elem of cfoChart.newDataBacklog) {
						for (let ant = 1; ant < cfoChart.count; ++ant) {
							let value = elem.cfos[ant - 1]
							if (!Number.isNaN(value)) {
								cfoChart.series(ant).append(elem.time, value)
								minY = Math.min(minY, value)
								maxY = Math.max(maxY, value)
							}
						}

						cfoXAxis.max = elem.time
						cfoXAxis.min = Math.max(0, elem.time - backend.maxCSIAge)
					}

					if (minY !== Infinity) {
						let padding = Math.max(10000, (maxY - minY) * 0.1)
						cfoHzAxis.min = minY - padding
						cfoHzAxis.max = maxY + padding
						cfoChart.updatePpmAxis()
					}

					cfoChart.newDataBacklog = []
				}
			}

			Timer {
				interval: 1000
				running: true
				repeat: true
				onTriggered: {
					for (let ant = 1; ant < cfoChart.count; ++ant) {
						let s = cfoChart.series(ant)
						if (s.count > 2) {
							let toRemoveCount = 0
							let now = s.at(s.count - 1).x
							for (; toRemoveCount < s.count; ++toRemoveCount) {
								if (now - s.at(toRemoveCount).x < backend.maxCSIAge)
									break
							}
							s.removePoints(0, toRemoveCount)
						}
					}
					cfoChart.updatePpmAxis()
				}
			}

			Connections {
				target: backend

				function onUpdateCFOs(time, cfos) {
					cfoChart.newDataBacklog.push({
						"time": time,
						"cfos": cfos
					})
				}

				function onChannelConfigChanged() {
					cfoChart.updatePpmAxis()
				}
			}

			Timer {
				interval: 1 / 30 * 1000
				running: true
				repeat: true
				onTriggered: {
					backend.update()
				}
			}
		}
	}
}
