import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import QtQuick
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 800
	minimumHeight: 500

	color: "#11191e"
	title: "Received Phases over Time"

	// Tab20 color cycle reordered: https://github.com/matplotlib/matplotlib/blob/main/lib/matplotlib/_cm.py#L1293
	property var colorCycle: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5", "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5"]
	property color textColor: "#DDDDDD"

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig

			Label { Layout.columnSpan: 2; text: "Display Settings"; color: "#9fb3c8" }

			Label { text: "Max Age (s)"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: maxAgeSpinBox
				property string configKey: "max_age"
				property string configProp: "value"
				property var encode: function(v) { return v }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 160
				from: 1
				to: 60
				value: 10
			}

			Label { text: "Shift Peak"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: shiftPeakSwitch
				property string configKey: "shift_peak"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 80
				checked: false
			}

			Label { text: "Ref. Antenna"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: referenceSpinBox
				property string configKey: "reference"
				property string configProp: "value"
				property var encode: function(v) { return v }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 160
				from: 0
				to: backend.sensorCount - 1
				value: 0
			}

			Common.GenericAppSettings {
				id: genericAppSettings
				insertBefore: genericAppSettingsAnchor
				implicitWidth: 160
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

			// Spacer
			Rectangle {
				id: endSpacer
				Layout.columnSpan: 2
				width: 1; height: 30
				color: "transparent"
			}
		}
	}

	Rectangle {
		anchors.fill: parent
		anchors.margins: 10
		color: "#151f26"
		opacity: 1

		ColumnLayout {
			anchors.fill: parent
			Layout.alignment: Qt.AlignCenter

			ChartView {
				Layout.alignment: Qt.AlignCenter

				id: calibrationPhasesOverTime
				legend.visible: false
				Layout.fillWidth: true
				Layout.fillHeight: true
				Layout.margins: 10

				antialiasing: true
				backgroundColor: "#11191e"

				property var newDataBacklog: Array()

				axes: [
					ValueAxis {
						id: calibrationPhasesOverTimeXAxis

						min: 0
						max: 20
						titleText: "<font color=\"#e0e0e0\">Time [s]</font>"
						titleFont.bold: false
						gridLineColor: "#c0c0c0"
						tickInterval: 5
						tickType: ValueAxis.TicksDynamic
						labelsColor: "#e0e0e0"
					},
					ValueAxis {
						id: calibrationPhasesOverTimeYAxis

						min: -Math.PI
						max: Math.PI
						titleText: "<font color=\"#e0e0e0\">Phase Difference [rad]</font>"
						titleFont.bold: false
						gridLineColor: "#c0c0c0"
						tickInterval: 1
						tickType: ValueAxis.TicksDynamic
						labelsColor: "#e0e0e0"
					}
				]

				Component.onCompleted : {
					for (let ant = 0; ant < backend.sensorCount; ++ant) {
						let phaseSeries = calibrationPhasesOverTime.createSeries(ChartView.SeriesTypeLine, "tx-" + ant, calibrationPhasesOverTimeXAxis, calibrationPhasesOverTimeYAxis)
						phaseSeries.pointsVisible = false
						phaseSeries.color = colorCycle[ant % colorCycle.length]
						phaseSeries.useOpenGL = true
					}
				}

				Timer {
					interval: 1 / 40 * 1000
					running: true
					repeat: true
					onTriggered: {
						for (const elem of calibrationPhasesOverTime.newDataBacklog) {
							for (let ant = 0; ant < calibrationPhasesOverTime.count; ++ant)
								calibrationPhasesOverTime.series(ant).append(elem.time, elem.phases[ant]);

							calibrationPhasesOverTimeXAxis.max = elem.time
							calibrationPhasesOverTimeXAxis.min = elem.time - backend.maxCSIAge
						}

						calibrationPhasesOverTime.newDataBacklog = []
					}
				}

				Timer {
					interval: 1 * 1000
					running: true
					repeat: true
					onTriggered: {
						// Count and delete series points which are too old
						for (let ant = 0; ant < calibrationPhasesOverTime.count; ++ant) {
							let s = calibrationPhasesOverTime.series(ant);
							if (s.count > 2) {
								let toRemoveCount = 0;
								let now = s.at(s.count - 1).x;
								for (; toRemoveCount < s.count; ++toRemoveCount) {
									if (now - s.at(toRemoveCount).x < backend.maxCSIAge)
										break;
								}
								s.removePoints(0, toRemoveCount);
							}
						}
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

				Connections {
					target: backend

					function onUpdatePhases(time, phases) {
						calibrationPhasesOverTime.newDataBacklog.push({
							"time" : time,
							"phases" : phases
						})
					}
				}
			}
		}
	}
}