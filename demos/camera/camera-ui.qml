import "."

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Controls.Material
import QtCharts
import "../common" as Common

Common.DemoApplication {
	id: window
	visible: true
	minimumWidth: 800
	minimumHeight: 500

	color: "#333333"
	title: "ESPARGOS Camera Overlay Demo"

	demoDrawerComponent: Component {
		Common.DemoDrawer {
			id: demoDrawer
			title: "Demo Settings"
			endpoint: democonfig

			// Match app-wide Material settings
			Material.theme: Material.Dark
			Material.primary: "#227b3d"
			Material.accent: "#227b3d"
			Material.roundedScale: Material.notRounded

			Label { Layout.columnSpan: 2; text: "Camera Input"; color: "#9fb3c8" }
			Label { text: "Device"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: cameraDevice
				property string configKey: "camera_device"
				property string configProp: "currentIndex"
				Component.onCompleted: demoDrawer.configManager.register(this)
				onCurrentIndexChanged: {
					demoDrawer.configManager.onControlChanged(this)

					// Update formats when device changes, always pick last available format by default
					cameraFormat.model = WebCam.availableFormats
					cameraFormat.currentIndex = cameraFormat.model.length - 1
				}
				implicitWidth: 210
				model: WebCam.availableDevices
				currentIndex: 0
				function isUserActive() { return pressed || popup.visible }
			}

			Label { text: "Format"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: cameraFormat
				property string configKey: "camera_format"
				property string configProp: "currentIndex"
				Component.onCompleted: {
					demoDrawer.configManager.register(this)

					// Populate formats for initial device
					cameraFormat.model = WebCam.availableFormats
					cameraFormat.currentIndex = cameraFormat.model.length - 1
				}
				onCurrentIndexChanged: demoDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				// Initially empty, populated when device changes or when component is completed
				model: []
				currentIndex: 0
				function isUserActive() { return pressed || popup.visible }
			}

			/*Label { text: "Flip"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: cameraFlip
				property string configKey: "camera_flip"
				property string configProp: "checked"
				Component.onCompleted: demoDrawer.configManager.register(this)
				onCheckedChanged: demoDrawer.configManager.onControlChanged(this)
				implicitWidth: 80
				checked: false
				function isUserActive() { return pressed }
			}

			Label { Layout.columnSpan: 2; text: "Beamforming"; color: "#9fb3c8" }
			Label { text: "Method"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: beamformerMode
				property string configKey: "beamformer_type"
				property string configProp: "currentIndex"
				property var encode: function(v) { return ["FFT", "Bartlett", "MVDR", "MUSIC"][v] }
				property var decode: function(v) {
					let idx = ["FFT", "Bartlett", "MVDR", "MUSIC"].indexOf(v)
					return idx >= 0 ? idx : 0
				}
				Component.onCompleted: demoDrawer.configManager.register(this)
				onCurrentIndexChanged: demoDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				model: [ "FFT", "Bartlett", "MVDR", "MUSIC" ]
				currentIndex: 0
				function isUserActive() { return pressed || popup.visible }
			}*/
		}
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
}