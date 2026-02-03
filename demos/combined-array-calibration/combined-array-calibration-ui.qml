import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import QtQuick

import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	title: "Combined Array Calibration Demo"

	// Tab20 color cycle reordered: https://github.com/matplotlib/matplotlib/blob/main/lib/matplotlib/_cm.py#L1293
	property var colorCycle: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5", "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5"]

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig

			Label { Layout.columnSpan: 2; text: "Calibration Settings"; color: "#9fb3c8" }

			Label { text: "Update Rate"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: updateRateSpinBox
				property string configKey: "update_rate"
				property string configProp: "value"
				property var encode: function(v) { return v / 1000.0 }
				property var decode: function(v) { return Math.round(Number(v) * 1000) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 160
				from: 1
				to: 100
				stepSize: 1
				value: 10
				textFromValue: function(value) { return (value / 1000).toFixed(3); }
				valueFromText: function(text) { return parseFloat(text) * 1000; }
			}

			Label { text: "Boardwise"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: boardwiseSwitch
				property string configKey: "boardwise"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 80
				checked: false
			}

			Label { text: "Color by Sensor"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: colorBySensorSwitch
				property string configKey: "color_by_sensor_index"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 80
				checked: false
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
		}
	}

	ColumnLayout {
		height: parent.height
		width: parent.width

		ChartView {
			id: csiPhase
			Layout.alignment: Qt.AlignTop
			legend.visible: false
			legend.labelColor: "#e0e0e0"
			Layout.fillWidth: true
			Layout.fillHeight: true
			antialiasing: true
			backgroundColor: "#151f26"

			axes: [
				ValueAxis {
					id: csiPhaseSubcarrierAxis

					min: backend.subcarrierRange.length > 0 ? backend.subcarrierRange[0] : -32
					max: backend.subcarrierRange.length > 0 ? backend.subcarrierRange[backend.subcarrierRange.length - 1] : 32
					titleText: "<font color=\"#e0e0e0\">Subcarrier Index</font>"
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickInterval: 20
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#e0e0e0"
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

			function rebuildSeries() {
				csiPhase.removeAllSeries();
				for (let ant = 0; ant < backend.sensorCount; ++ant) {
					let series = csiPhase.createSeries(ChartView.SeriesTypeLine, "tx-" + ant, csiPhaseSubcarrierAxis, csiPhaseAxis);
					series.pointsVisible = false;
					const sensorIndexInBoard = ant % backend.sensorCountPerBoard;
					const boardIndex = ~~(ant / backend.sensorCountPerBoard);
					const colorIndex = (backend.colorBySensorIndex ? sensorIndexInBoard : boardIndex) % colorCycle.length;
					series.color = colorCycle[colorIndex];
					series.useOpenGL = true;

					for (const s of backend.subcarrierRange) {
						series.append(s, 0)
					}
				}
			}

			Connections {
				target: backend
				function onSensorCountChanged() {
					csiPhase.rebuildSeries();
				}
				function onInitComplete() {
					csiPhase.rebuildSeries();
				}
				function onColorBySensorIndexChanged() {
					csiPhase.rebuildSeries();
				}
			}
		}
	}

	Timer {
		interval: 1 / 60 * 1000
		running: !backend.initializing
		repeat: true
		onTriggered: {
			if (backend.sensorCount === 0)
				return;

			let phaseSeries = [];
			for (let i = 0; i < backend.sensorCount; ++i)
				phaseSeries.push(csiPhase.series(i));

			backend.updateCalibrationResult(phaseSeries)
		}
	}
}