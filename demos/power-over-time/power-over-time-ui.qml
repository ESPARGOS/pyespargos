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
	title: "Power over Time"

	property var colorCycle: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5", "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5"]

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig

			Label { Layout.columnSpan: 2; text: "Power Settings"; color: "#9fb3c8" }

			Label { text: "Mode"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: modeCombo
				property string configKey: "mode"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180

				model: [
					{ value: "sum", text: "All Subcarriers" },
					{ value: "subcarrier", text: "Single Subcarrier" }
				]
				textRole: "text"
				valueRole: "value"
				currentValue: "sum"
			}

			Label { text: "Sensors"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: sensorModeCombo
				property string configKey: "sensor_mode"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180

				model: [
					{ value: "all", text: "All Sensors" },
					{ value: "single", text: "Single Sensor" },
					{ value: "sum", text: "Sum of Powers" }
				]
				textRole: "text"
				valueRole: "value"
				currentValue: "all"
			}

			Label { text: "Sensor Index"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: backend.sensorMode === "single" }
			SpinBox {
				id: selectedSensorSpinBox
				property string configKey: "selected_sensor"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180
				from: 0
				to: Math.max(0, backend.sensorCount - 1)
				value: 0
				visible: backend.sensorMode === "single"
			}

			Label { text: "Subcarrier"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: backend.mode === "subcarrier" }
			SpinBox {
				id: subcarrierSpinBox
				property string configKey: "subcarrier"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180
				from: backend.minSubcarrierIndex
				to: backend.maxSubcarrierIndex
				value: 0
				visible: backend.mode === "subcarrier"
			}

			Label { text: "Scale"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: scaleCombo
				property string configKey: "scale"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180

				model: [
					{ value: "log", text: "Logarithmic" },
					{ value: "linear", text: "Linear" }
				]
				textRole: "text"
				valueRole: "value"
				currentValue: "log"
			}

			Label { text: "Max Age (s)"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: maxAgeSpinBox
				property string configKey: "max_age"
				property string configProp: "value"
				property var encode: function(v) { return v }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180
				from: 1
				to: 60
				value: 10
			}

			Label { text: "Required Antennas"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: requiredAntennasSpinBox
				property string configKey: "required_antennas"
				property string configProp: "value"
				property var encode: function(v) { return v }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180
				from: 1
				to: backend.sensorCount
				value: backend.sensorCount
			}

			Common.GenericAppSettings {
				id: genericAppSettings
				insertBefore: genericAppSettingsAnchor
				controlWidth: 180
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
				id: endSpacer
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

		ColumnLayout {
			anchors.fill: parent

				ChartView {
					id: amplitudesOverTime
					legend.visible: false
				Layout.fillWidth: true
				Layout.fillHeight: true
				Layout.margins: 10
				antialiasing: true
					backgroundColor: "#11191e"

					property var newDataBacklog: Array()
					property bool logScale: backend.scale === "log"

				axes: [
					ValueAxis {
						id: amplitudesOverTimeXAxis

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
						id: amplitudesOverTimeYAxis

							min: amplitudesOverTime.logScale ? -120 : 0
							max: amplitudesOverTime.logScale ? 20 : 1
							titleText: amplitudesOverTime.logScale
								? "<font color=\"#e0e0e0\">Power [dB]</font>"
								: "<font color=\"#e0e0e0\">Power [linear]</font>"
						titleFont.bold: false
						gridLineColor: "#c0c0c0"
						tickAnchor: 0
							tickInterval: amplitudesOverTime.logScale
								? 5
								: Math.max(1, Math.pow(10, Math.floor(Math.log10(Math.max(1, max - min)))))
							tickType: ValueAxis.TicksDynamic
							labelsColor: "#e0e0e0"
					}
				]

				Component.onCompleted : {
					for (let ant = 0; ant < backend.sensorCount; ++ant) {
						let amplitudeSeries = amplitudesOverTime.createSeries(ChartView.SeriesTypeLine, "rx-" + ant, amplitudesOverTimeXAxis, amplitudesOverTimeYAxis)
						amplitudeSeries.pointsVisible = false
						amplitudeSeries.color = colorCycle[ant % colorCycle.length]
						amplitudeSeries.useOpenGL = false
					}
					refreshSeriesVisibility()
				}

					function refreshSeriesVisibility() {
						for (let ant = 0; ant < amplitudesOverTime.count; ++ant) {
							let s = amplitudesOverTime.series(ant);
							if (backend.sensorMode === "sum") {
								s.visible = ant === 0;
							} else if (backend.sensorMode === "single") {
								s.visible = ant === backend.selectedSensor;
							} else {
								s.visible = true;
							}
						}
					}

					function resetChart(refreshVisibility = false) {
						clearSeriesData()
						if (refreshVisibility)
							refreshSeriesVisibility()
					}

					function clearSeriesData() {
						for (let ant = 0; ant < amplitudesOverTime.count; ++ant) {
						amplitudesOverTime.series(ant).clear();
						}
						amplitudesOverTime.newDataBacklog = [];
						amplitudesOverTimeXAxis.min = 0;
						amplitudesOverTimeXAxis.max = Math.max(backend.maxCSIAge, 1);
						amplitudesOverTimeYAxis.min = amplitudesOverTime.logScale ? -120 : 0;
						amplitudesOverTimeYAxis.max = amplitudesOverTime.logScale ? 20 : 1;
					}

				Timer {
					interval: 1 / 40 * 1000
					running: true
					repeat: true
					onTriggered: {
						for (const elem of amplitudesOverTime.newDataBacklog) {
							for (let ant = 0; ant < backend.sensorCount; ++ant) {
								const value = elem.amplitudes[ant];
								if (!Number.isFinite(elem.time) || !Number.isFinite(value))
									continue;
								amplitudesOverTime.series(ant).append(elem.time, value);
							}

							if (Number.isFinite(elem.time)) {
								amplitudesOverTimeXAxis.max = elem.time
								amplitudesOverTimeXAxis.min = elem.time - backend.maxCSIAge
							}
							if (Number.isFinite(elem.ymin) && Number.isFinite(elem.ymax) && elem.ymax > elem.ymin) {
								amplitudesOverTimeYAxis.min = elem.ymin
								amplitudesOverTimeYAxis.max = elem.ymax
							}
						}

						amplitudesOverTime.newDataBacklog = []
					}
				}

				Timer {
					interval: 1000
					running: true
					repeat: true
					onTriggered: {
						for (let ant = 0; ant < amplitudesOverTime.count; ++ant) {
							let s = amplitudesOverTime.series(ant);
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
					onTriggered: backend.update()
				}

				Connections {
					target: backend

					function onUpdatePowers(time, amplitudes, ymin, ymax) {
						amplitudesOverTime.newDataBacklog.push({
							"time": time,
							"amplitudes": amplitudes,
							"ymin": ymin,
							"ymax": ymax
						})
					}

						function onSensorModeChanged() {
							amplitudesOverTime.resetChart(true)
						}

						function onSelectedSensorChanged() {
							amplitudesOverTime.resetChart(true)
						}

						function onModeChanged() {
							backend.clampSubcarrierToRange()
							amplitudesOverTime.resetChart()
						}

						function onScaleChanged() {
							amplitudesOverTime.resetChart()
						}

						function onPreambleFormatChanged() {
							backend.clampSubcarrierToRange()
							amplitudesOverTime.resetChart()
						}
					}
			}
		}
	}
}
