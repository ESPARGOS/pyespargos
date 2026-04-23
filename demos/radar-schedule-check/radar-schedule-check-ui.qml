import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Controls.Material
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 980
	minimumHeight: 640

	color: "#11191e"
	title: "Radar Schedule Check"

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Radar Test Settings"
			endpoint: appconfig

			Label { Layout.columnSpan: 2; text: "Radar Schedule"; color: "#9fb3c8" }

			Label { text: "Start [us]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				property string configKey: "start_us"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 0
				to: 1000000
				value: 0
				stepSize: 100
				implicitWidth: 200
			}

			Label { text: "Slot [us]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				property string configKey: "slot_us"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 1
				to: 1000000
				value: 10000
				stepSize: 100
				implicitWidth: 200
			}

			Label { text: "Period [us]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				property string configKey: "period_us"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 1000
				to: 1000000
				value: 80000
				stepSize: 1000
				implicitWidth: 200
			}

			Label { text: "Stats Window"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				property string configKey: "history_packets"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 5
				to: 1000
				value: 100
				stepSize: 5
				implicitWidth: 200
			}

			Label { text: "TX Power"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				property string configKey: "tx_power"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 0
				to: 255
				value: 34
				implicitWidth: 200
			}

			Label { text: "PHY Mode"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				property string configKey: "tx_phymode"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 0
				to: 16
				value: 2
				implicitWidth: 200
			}

			Label { text: "Rate"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				property string configKey: "tx_rate"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 0
				to: 64
				value: 11
				implicitWidth: 200
			}

			Label { text: "Radar RF Switch"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				property string configKey: "rfswitch_state"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 0
				to: 4
				value: 2
				implicitWidth: 200
			}

			Button {
				Layout.columnSpan: 2
				Layout.fillWidth: true
				text: "Apply Radar Schedule"
				onClicked: backend.applyRadarSchedule()
			}

			Button {
				Layout.columnSpan: 2
				Layout.fillWidth: true
				text: "Disable Radar"
				onClicked: backend.disableRadarSchedule()
			}
		}
	}

	ColumnLayout {
		anchors.fill: parent
		anchors.margins: 36
		spacing: 18

		Label {
			text: backend.statusText
			color: "#ffffff"
			font.pixelSize: 22
		}

		Label {
			text: backend.latestSource
			color: "#9fb3c8"
			font.pixelSize: 16
		}

		Label {
			text: "Radar packets processed: " + backend.packetCount
			color: "#9fb3c8"
			font.pixelSize: 16
		}

		Label {
			text: "Rolling mean error and standard deviation to the expected transmit time in the common reference clock"
			color: "#d9e6ee"
			font.pixelSize: 14
		}

		Rectangle {
			Layout.fillWidth: true
			Layout.minimumHeight: 210
			radius: 8
			color: "#0d1418"
			border.color: "#2e4656"
			border.width: 1

			ScrollView {
				anchors.fill: parent
				anchors.margins: 12
				clip: true

				Label {
					text: backend.txRxTimestampTableText
					color: "#b7f7c0"
					font.pixelSize: 13
					font.family: "monospace"
				}
			}
		}

		GridLayout {
			Layout.fillWidth: true
			Layout.fillHeight: true
			columns: 4
			columnSpacing: 14
			rowSpacing: 14

			Repeater {
				model: backend.sensorCount

				Rectangle {
					Layout.fillWidth: true
					Layout.minimumWidth: 150
					Layout.minimumHeight: 110
					radius: 8
					color: "#1a252d"
					border.color: "#2e4656"
					border.width: 1

					Column {
						anchors.centerIn: parent
						spacing: 8

						Label {
							text: "Sensor " + index.toString().padStart(2, " ")
							color: "#ffffff"
							font.pixelSize: 18
							font.family: "monospace"
							horizontalAlignment: Text.AlignHCenter
						}

						Label {
							text: backend.radarResidualTexts[index]
							color: "#8be28b"
							font.pixelSize: 22
							font.bold: true
							font.family: "monospace"
							horizontalAlignment: Text.AlignHCenter
						}
					}
				}
			}
		}
	}
}
