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

	color: "#101817"
	title: "Radar CSI"

	property var colorCycle: ["#2e7d32", "#80cbc4", "#ffb300", "#ef5350", "#42a5f5", "#c0ca33", "#ff7043", "#ab47bc", "#26a69a", "#d4e157", "#8d6e63", "#29b6f6"]
	property var amplitudeSeries: []
	property var phaseSeries: []

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Radar CSI Settings"
			endpoint: appconfig

			Label { Layout.columnSpan: 2; text: "Radar Schedule"; color: "#92b8ad" }

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
					from: -5000
					to: 5000
					value: 1063
					stepSize: 1
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
			backgroundColor: "#14211f"

			axes: [
				ValueAxis {
					id: csiAmplitudeSubcarrierAxis
					min: -Math.floor(backend.subcarrierCount / 2)
					max: Math.floor(backend.subcarrierCount / 2) - 1
					titleText: "<font color=\"#dce8e4\">Subcarrier Index</font>"
					gridLineColor: "#41524d"
					tickInterval: 20
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#dce8e4"
					labelFormat: "%d"
				},
				ValueAxis {
					id: csiAmplitudeAxis
					min: -70
					max: 70
					titleText: "<font color=\"#dce8e4\">Power [dB]</font>"
					gridLineColor: "#41524d"
					tickInterval: 10
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#dce8e4"
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
			backgroundColor: "#14211f"

			axes: [
				ValueAxis {
					id: csiPhaseSubcarrierAxis
					min: -Math.floor(backend.subcarrierCount / 2)
					max: Math.floor(backend.subcarrierCount / 2) - 1
					titleText: "<font color=\"#dce8e4\">Subcarrier Index</font>"
					gridLineColor: "#41524d"
					tickInterval: 20
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#dce8e4"
					labelFormat: "%d"
				},
				ValueAxis {
					id: csiPhaseAxis
					min: -3.14
					max: 3.14
					titleText: "<font color=\"#dce8e4\">Phase [rad]</font>"
					gridLineColor: "#41524d"
					tickInterval: 2
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#dce8e4"
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
