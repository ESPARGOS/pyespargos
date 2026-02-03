import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 800
	minimumHeight: 500

	color: "#11191e"
	title: "Combined Array Demo"

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig

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

			// Spacer
			Rectangle {
				id: endSpacer
				Layout.columnSpan: 2
				width: 1; height: 30
				color: "transparent"
			}
		}
	}

	Item {
		anchors.centerIn: parent
		width: childrenRect.width
		height: childrenRect.height

		ColumnLayout {
			id: antennaArray
			Layout.alignment: Qt.AlignHCenter
			spacing: -2

			Repeater {
				model: backend.numberOfRows

				RowLayout {
					spacing: -2

					Repeater {
						model: backend.numberOfColumns

						Rectangle {
							id: antennaRect
							implicitWidth: window.width / (Math.max(backend.numberOfRows, backend.numberOfColumns) + 2)
							implicitHeight: window.width / (Math.max(backend.numberOfRows, backend.numberOfColumns) + 2)
							color: "#cd4141"
							border.width: 2
							border.color: antennaArray.isPrimaryTransmitter ? "#33ff33" : "#ffffff"
						}
					}
				}
			}
		}
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

		function onUpdateColors(colors) {
			for (var row = 0; row < backend.numberOfRows; row++) {
				for (var col = 0; col < backend.numberOfColumns; col++) {
					let phaseRect = antennaArray.children[row].children[col]
					if (phaseRect instanceof Rectangle) {
						phaseRect.color = Qt.rgba(...colors[row * backend.numberOfColumns + col])
					}
				}
			}
		}
	}
}