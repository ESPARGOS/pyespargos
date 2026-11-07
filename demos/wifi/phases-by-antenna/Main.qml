import "../common" as WiFi
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import "../../common" as Common

Common.ESPARGOSApplication {
	id: window
	receiverDrawerComponent: Component { WiFi.WiFiTransceiverDrawer {} }
	visible: true
	minimumWidth: 800
	minimumHeight: 500

	color: "#11191e"
	title: "Phases by Antenna"

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig

			WiFi.CSIAppSettings {
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

			WiFi.CSIBacklogSettings {
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

    Common.PhaseArrays {
        id: antennaArray
        anchors.fill: parent
        rowCount: backend.numberOfRows
        columns: backend.numberOfColumns
    }

	Timer {
		interval: 1 / 60 * 1000
		running: true
		repeat: true
		onTriggered: {
			backend.updateRequest()
		}
	}

	Connections {
		target: backend

        function onUpdatePhases(panels) { antennaArray.panels = panels }
    }
}
