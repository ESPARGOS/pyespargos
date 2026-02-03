import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick
import QtCharts
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 800
	minimumHeight: 500

	color: "#11191e"
	title: "Instantaneous CSI: " + (backend.timeDomain ? "Time Domain" : (backend.superResolution ? "Superresolution Time Domain" : "Frequency Domain"))

	// Tab20 color cycle reordered: https://github.com/matplotlib/matplotlib/blob/main/lib/matplotlib/_cm.py#L1293
	property var colorCycle: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5", "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5"]

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig


			Label { Layout.columnSpan: 2; text: "Display Settings"; color: "#9fb3c8" }

			Label { text: "Display Mode"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: displayModeCombo
				property string configKey: "display_mode"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 210

				model: [
					{ value: "frequency", text: "Frequency Domain" },
					{ value: "timedomain", text: "Time Domain" },
					{ value: "music", text: "MUSIC" },
					{ value: "mvdr", text: "MVDR" }
				]
				textRole: "text"
				valueRole: "value"
				currentValue: "frequency"
			}

			Label { text: "Oversampling"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: backend.timeDomain }
			SpinBox {
				id: oversamplingSpinBox
				property string configKey: "oversampling"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				from: 1
				to: 16
				value: 4
				visible: backend.timeDomain
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
				id: backlogSettingsAnchor;
				Layout.columnSpan: 2;
				width: 0;
				height: 0;
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

	ColumnLayout {
		height: parent.height
		width: parent.width

		ChartView {
			id: csiAmplitude
			Layout.alignment: Qt.AlignTop
			legend.visible: false
			legend.labelColor: "#e0e0e0"
			Layout.fillWidth: true
			Layout.fillHeight: true
			antialiasing: true
			backgroundColor: "#151f26"

			axes: [
				ValueAxis {
					id: csiAmplitudeSubcarrierAxis

					min: (backend.timeDomain || backend.superResolution) ? -7 : -Math.floor(backend.subcarrierCount / 2)
					max: (backend.timeDomain || backend.superResolution) ? 7 : Math.floor(backend.subcarrierCount / 2) - 1
					titleText: (backend.timeDomain || backend.superResolution) ? "<font color=\"#e0e0e0\">Delay [tap]</font>" : "<font color=\"#e0e0e0\">Subcarrier Index</font>"
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickInterval: backend.superResolution ? 1 : 20
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#e0e0e0"
					labelFormat: "%d"
				},
				ValueAxis {
					id: csiAmplitudeAxis

					min: 0
					max: 0.02
					titleText: (backend.timeDomain || backend.superResolution) ? "<font color=\"#e0e0e0\">Power [linear]</font>" : "<font color=\"#e0e0e0\">Power [dB]</font>"
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickInterval: backend.timeDomain ? 100000 : (backend.superResolution ? 0.5 : 5)
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#e0e0e0"
				}
			]

			Component.onCompleted : {
				for (let ant = 0; ant < backend.sensorCount; ++ant) {
					let series = csiAmplitude.createSeries(ChartView.SeriesTypeLine, "tx-" + ant, csiAmplitudeSubcarrierAxis, csiAmplitudeAxis)
					series.pointsVisible = false
					series.color = colorCycle[ant % colorCycle.length]
					series.useOpenGL = true
				}
			}
		}

		ChartView {
			id: csiPhase
			Layout.alignment: Qt.AlignTop
			legend.visible: false
			legend.labelColor: "#e0e0e0"
			Layout.fillWidth: true
			Layout.fillHeight: true
			antialiasing: true
			backgroundColor: "#151f26"
			visible: !backend.superResolution

			axes: [
				ValueAxis {
					id: csiPhaseSubcarrierAxis

					min: (backend.timeDomain || backend.superResolution) ? -7 : -Math.floor(backend.subcarrierCount / 2)
					max: (backend.timeDomain || backend.superResolution) ? 7 : Math.floor(backend.subcarrierCount / 2) - 1
					titleText: (backend.timeDomain || backend.superResolution) ? "<font color=\"#e0e0e0\">Delay [tap]</font>" : "<font color=\"#e0e0e0\">Subcarrier Index</font>"
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickInterval: backend.superResolution ? 5 : 20
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

			Component.onCompleted : {
				for (let ant = 0; ant < backend.sensorCount; ++ant) {
					let series = csiPhase.createSeries(ChartView.SeriesTypeLine, "tx-" + ant, csiPhaseSubcarrierAxis, csiPhaseAxis)
					series.pointsVisible = false
					series.color = colorCycle[ant % colorCycle.length]
					series.useOpenGL = true
				}
			}
		}
	}

	Timer {
		interval: (backend.superResolution ? 1 / 30 : 1 / 60) * 1000
		running: true
		repeat: true
		onTriggered: {
			let amplitudeSeries = [];
			let phaseSeries = [];
			for (let i = 0; i < backend.sensorCount; ++i) {
				amplitudeSeries.push(csiAmplitude.series(i));
				phaseSeries.push(csiPhase.series(i));
			}

			backend.updateCSI(amplitudeSeries, phaseSeries, csiAmplitudeSubcarrierAxis, csiAmplitudeAxis)
		}
	}
}