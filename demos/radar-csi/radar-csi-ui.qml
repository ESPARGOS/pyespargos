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

			Label { text: "Period [us]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			RowLayout {
				spacing: 10
				Slider {
					id: periodSlider
					property string configKey: "period_us"
					property string configProp: "value"
					property var encode: function(v) { return Math.round(v) }
					property var decode: function(v) { return Math.max(10000, Math.min(500000, parseInt(v || 80000))) }
					Component.onCompleted: appDrawer.configManager.register(this)
					onValueChanged: appDrawer.configManager.onControlChanged(this)
					from: 10000
					to: 500000
					value: 80000
					stepSize: 1000
					implicitWidth: 145
					function isUserActive() { return pressed }
				}
				Label { text: Math.round(periodSlider.value); color: "#ffffff"; font.family: "monospace"; Layout.preferredWidth: 56; horizontalAlignment: Text.AlignRight }
			}

			Label { text: "Start [us]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			RowLayout {
				spacing: 10
				Slider {
					id: startSlider
					property string configKey: "start_us"
					property string configProp: "value"
					property var encode: function(v) { return Math.round(v) }
					property var decode: function(v) { return Math.max(0, Math.min(200000, parseInt(v || 10000))) }
					Component.onCompleted: appDrawer.configManager.register(this)
					onValueChanged: appDrawer.configManager.onControlChanged(this)
					from: 0
					to: 200000
					value: 10000
					stepSize: 1000
					implicitWidth: 145
					function isUserActive() { return pressed }
				}
				Label { text: Math.round(startSlider.value); color: "#ffffff"; font.family: "monospace"; Layout.preferredWidth: 56; horizontalAlignment: Text.AlignRight }
			}

			Label { text: "Slot [us]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			RowLayout {
				spacing: 10
				Slider {
					id: slotSlider
					property string configKey: "slot_us"
					property string configProp: "value"
					property var encode: function(v) { return Math.round(v) }
					property var decode: function(v) { return Math.max(100, Math.min(100000, parseInt(v || 10000))) }
					Component.onCompleted: appDrawer.configManager.register(this)
					onValueChanged: appDrawer.configManager.onControlChanged(this)
					from: 100
					to: 100000
					value: 10000
					stepSize: 100
					implicitWidth: 145
					function isUserActive() { return pressed }
				}
				Label { text: Math.round(slotSlider.value); color: "#ffffff"; font.family: "monospace"; Layout.preferredWidth: 56; horizontalAlignment: Text.AlignRight }
			}

			Label { text: "TX Correction Sign"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				property string configKey: "tx_correction_sign"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				model: [
					{ value: 1, text: "+1" },
					{ value: -1, text: "-1" }
				]
				textRole: "text"
				valueRole: "value"
				currentValue: 1
			}

			Label { text: "TX Timestamp Offset [ns]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			RowLayout {
				spacing: 10
				Slider {
					id: txOffsetSlider
					property string configKey: "tx_timestamp_offset_ns"
					property string configProp: "value"
					property var encode: function(v) { return Math.round(v) }
					property var decode: function(v) { return Math.max(-5000, Math.min(5000, parseInt(v || 1063))) }
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

			Button {
				Layout.columnSpan: 2
				text: "Apply Radar Schedule"
				onClicked: backend.applyRadarSchedule()
			}

			Button {
				Layout.columnSpan: 2
				text: "Disable Radar TX"
				onClicked: backend.disableRadarSchedule()
			}

			Label { Layout.columnSpan: 2; text: "FTM-style TX Offsets"; color: "#92b8ad" }

			TextArea {
				Layout.columnSpan: 2
				Layout.fillWidth: true
				Layout.preferredHeight: 54
				text: backend.txOffsetTableText
				readOnly: true
				wrapMode: Text.NoWrap
				color: "#dce8e4"
				font.family: "monospace"
				font.pixelSize: 11
				background: Rectangle { color: "#0b1211"; radius: 4 }
			}

			RowLayout {
				Layout.columnSpan: 2
				Layout.fillWidth: true
				spacing: 8
				Button {
					text: "Estimate"
					Layout.fillWidth: true
					onClicked: backend.estimatePerTxOffsets()
				}
				Button {
					text: "Reset"
					Layout.fillWidth: true
					onClicked: backend.resetPerTxOffsets()
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

		ScrollView {
			Layout.fillWidth: true
			Layout.preferredHeight: 150
			clip: true

			TextArea {
				text: backend.residualDelayTableText
				readOnly: true
				selectByMouse: true
				wrapMode: Text.NoWrap
				color: "#dce8e4"
				font.family: "monospace"
				font.pixelSize: 12
				background: Rectangle { color: "#0b1211"; radius: 4 }
			}
		}
	}

	Timer {
		id: updateTimer
		interval: 100
		running: true
		repeat: true
		onTriggered: {
			backend.updateCSI(amplitudeSeries, phaseSeries, csiAmplitudeAxis)
		}
	}
}
