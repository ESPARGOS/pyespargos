import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts

ApplicationWindow {
	id: window
	visible: true
	minimumWidth: 800
	minimumHeight: 500

	color: "#11191e"
	title: "Spatial Phase Distribution Demo"

	Item {
		anchors.centerIn: parent
		width: childrenRect.width
		height: childrenRect.height

		ColumnLayout {
			id: antennaArray
			Layout.alignment: Qt.AlignHCenter
			spacing: -2

			Repeater {
				model: 2

				RowLayout {
					spacing: -2

					Repeater {
						model: 4

						Rectangle {
							id: antennaRect
							implicitWidth: window.width / 6
							implicitHeight: window.width / 6
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
			for (var row = 0; row < antennaArray.children.length; row++) {
				for (var col = 0; col < antennaArray.children[row].children.length; col++) {
					let phaseRect = antennaArray.children[row].children[col]
					if (phaseRect instanceof Rectangle) {
						phaseRect.color = Qt.rgba(...colors[row * 4 + col])
					}
				}
			}
		}
	}
}