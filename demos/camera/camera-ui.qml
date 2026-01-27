import "."

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Controls.Material
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

			Label { Layout.columnSpan: 2; text: "Camera Input"; color: "#9fb3c8" }
			Label { text: "Device"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: cameraDevice
				property string configKey: "camera.device"
				property string configProp: "currentIndex"
				property var encode: function(v) { return cameraDevice.model[v] }
				property var decode: function(v) {
					let idx = cameraDevice.model.indexOf(v)
					return idx >= 0 ? idx : 0
				}
				Component.onCompleted: demoDrawer.configManager.register(this)
				onCurrentIndexChanged: demoDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				model: WebCam.availableDevices
				currentIndex: 0
				function isUserActive() { return pressed || popup.visible }
				delegate: ItemDelegate {
					width: cameraDevice.width
					text: modelData

					ToolTip.visible: hovered
					ToolTip.text: modelData
				}
			}

			Label { text: "Format"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: cameraFormat
				property string configKey: "camera.format"
				property string configProp: "currentIndex"
				property var encode: function(v) { return cameraFormat.model[v] }
				property var decode: function(v) {
					let idx = cameraFormat.model.indexOf(v)
					return idx >= 0 ? idx : 0
				}
				Component.onCompleted: {
					demoDrawer.configManager.register(this)

					// Populate formats for initial device
					cameraFormat.model = WebCam.availableFormats
				}
				onCurrentIndexChanged: demoDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				// Initially empty, populated when device changes or when component is completed
				model: []
				currentIndex: 0
				function isUserActive() { return pressed || popup.visible }

				Connections {
					target: WebCam
					function onAvailableFormatsChanged() {
						// Update formats when available formats change, always pick last available format by default
						cameraFormat.model = WebCam.availableFormats
						cameraFormat.currentIndex = cameraFormat.model.length - 1
					}
				}
			}

			Label { text: "Flip"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: cameraFlip
				property string configKey: "camera.flip"
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
				id: beamformerType
				property string configKey: "beamformer.type"
				property string configProp: "currentIndex"
				property var encode: function(v) { return ["FFT", "Bartlett", "MVDR", "MUSIC"][v] }
				property var decode: function(v) {
					let idx = ["FFT", "Bartlett", "MVDR", "MUSIC"].indexOf(v)
					return idx >= 0 ? idx : 0
				}
				Component.onCompleted: demoDrawer.configManager.register(this)
				onCurrentIndexChanged: demoDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				// Different internal representation than displayed strings
				model: [
					{ value: "FFT", text: "FFT"},
					{ value: "Bartlett", text: "Bartlett (like FFT)"},
					{ value: "MVDR", text: "MVDR"},
					{ value: "MUSIC", text: "MUSIC"}
				]
				textRole: "text"
				valueRole: "value"
				currentIndex: 0
				function isUserActive() { return pressed || popup.visible }
			}
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