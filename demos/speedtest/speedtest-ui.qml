import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 600
	minimumHeight: 400

	color: "#11191e"
	title: "CSI Speedtest"

	property color textColor: "#DDDDDD"

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig

			Label { Layout.columnSpan: 2; text: "Predicate Settings"; color: "#9fb3c8" }

			Label { text: "Min. Antennas"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: minAntennasSpinBox
				property string configKey: "min_antennas"
				property string configProp: "value"
				property var encode: function(v) { return v }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 160
				from: 1
				to: backend.totalAntennas
				value: 1
			}

			Item {
				id: genericAppSettingsAnchor
				Layout.columnSpan: 2
				width: 0
				height: 0
			}
		}
	}

	// Central throughput display
	Column {
		anchors.centerIn: parent
		spacing: 10
		visible: !backend.initializing

		Label {
			text: backend.throughput.toFixed(1)
			font.pixelSize: Math.max(80, window.width / 8)
			font.bold: true
			color: "#ffffff"
			anchors.horizontalCenter: parent.horizontalCenter
			horizontalAlignment: Text.AlignHCenter
		}

		Label {
			text: "packets / second"
			font.pixelSize: Math.max(20, window.width / 40)
			color: "#9fb3c8"
			anchors.horizontalCenter: parent.horizontalCenter
			horizontalAlignment: Text.AlignHCenter
		}

		Label {
			text: "Predicate: ≥ " + backend.minAntennas + " of " + backend.totalAntennas + " antennas"
			font.pixelSize: Math.max(14, window.width / 60)
			color: "#667788"
			anchors.horizontalCenter: parent.horizontalCenter
			horizontalAlignment: Text.AlignHCenter
			topPadding: 20
		}
	}
}
