import "."

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import "../common" as Common

Common.DemoApplication {
	id: window
	visible: true
	minimumWidth: 800
	minimumHeight: 500

	color: "#333333"
	title: "ESPARGOS Camera Overlay Demo"

	// Full screen management
	visibility: ApplicationWindow.Windowed
	Shortcut {
		sequence: "F11"
		onActivated: {
			if (window.visibility == ApplicationWindow.Windowed) {
				window.visibility = ApplicationWindow.FullScreen
				footer.visible = false
			} else {
				window.visibility = ApplicationWindow.Windowed
				footer.visible = true
			}
		}
	}

	Shortcut {
		sequence: "Esc"
		onActivated: window.close()
	}


	CameraOverlay {
		anchors.fill: parent

		Rectangle {
			height: parent.height * 0.8
			visible: backend.manualExposure
			width: 40
			color: "#20ffffff"
			anchors.right: parent.right
			anchors.rightMargin: 20
			anchors.verticalCenter: parent.verticalCenter
			radius: 10

			Slider {
				id: exposureSlider
				anchors.fill: parent
				anchors.topMargin: 20
				anchors.bottomMargin: 20
				orientation: Qt.Vertical
				from: 0
				to: 1
				value: 0.5

				handle: Rectangle {
					x: exposureSlider.leftPadding + exposureSlider.availableWidth / 2 - width / 2
					y: exposureSlider.topPadding +  exposureSlider.visualPosition * (exposureSlider.availableHeight - height)
					implicitWidth: 26
					implicitHeight: 26
					radius: 12
					color: exposureSlider.pressed ? "#f0f0f0" : "#f6f6f6"
					border.color: "#bdbebf"
				}

				onMoved : {
					backend.adjustExposure(value);
				}

				Component.onCompleted : {
					backend.adjustExposure(value);
				}
			}
		}
	}

	footer: Pane {
		RowLayout {
			visible: WebCam.hasVideoInput
			anchors.fill: parent

			Text {
				text: WebCam.description + ' [Live]'
			}

			Text {
				text: WebCam.resolution.width + 'x' + WebCam.resolution.height
				Layout.alignment: Qt.AlignRight
			}
		}
	}
}