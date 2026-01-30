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

			CheckBox {
				id: advancedSettings
				Layout.columnSpan: 2
				text: "Show advanced"
				checked: false
				indicator.width: 18
				indicator.height: 18
			}

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
			}

			Label { text: "FOV Azi"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: advancedSettings.checked }
			Slider {
				id: fovAzimuth
				property string configKey: "camera.fov_azimuth"
				property string configProp: "value"
				property var encode: function(v) { return Math.round(v) }
				property var decode: function(v) { return Number(v) }
				from: 10
				to: 179
				stepSize: 1
				implicitWidth: 210
				Component.onCompleted: demoDrawer.configManager.register(this)
				onValueChanged: demoDrawer.configManager.onControlChanged(this)
				value: 72
				ToolTip.visible: hovered
				ToolTip.text: "" + Math.round(value) + "°"
				visible: advancedSettings.checked
			}

			Label { text: "FOV Ele"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: advancedSettings.checked }
			Slider {
				id: fovElevation
				property string configKey: "camera.fov_elevation"
				property string configProp: "value"
				property var encode: function(v) { return Math.round(v) }
				property var decode: function(v) { return Number(v) }
				from: 10
				to: 120
				stepSize: 1
				implicitWidth: 210
				Component.onCompleted: demoDrawer.configManager.register(this)
				onValueChanged: demoDrawer.configManager.onControlChanged(this)
				value: 41
				ToolTip.visible: hovered
				ToolTip.text: "" + Math.round(value) + "°"
				visible: advancedSettings.checked
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
				onCurrentIndexChanged: {
					// colorize_delay only makes sense for FFT beamformer, set to false when changing away
					if (beamformerType.currentIndex !== 0) {
						colorizeDelay.currentIndex = 0
					}
					demoDrawer.configManager.onControlChanged(this)
				}
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
			}

			Label { text: "Res Azi"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: advancedSettings.checked }
			Slider {
				id: resolutionAzimuth
				property string configKey: "beamformer.resolution_azimuth"
				property string configProp: "value"
				property var encode: function(v) { return Math.round(v) }
				property var decode: function(v) { return Number(v) }
				from: 4
				to: 128
				stepSize: 1
				implicitWidth: 210
				Component.onCompleted: demoDrawer.configManager.register(this)
				onValueChanged: demoDrawer.configManager.onControlChanged(this)
				value: 64
				ToolTip.visible: hovered
				ToolTip.text: "" + Math.round(value)
				visible: advancedSettings.checked
			}

			Label { text: "Res Ele"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: advancedSettings.checked }
			Slider {
				id: resolutionElevation
				property string configKey: "beamformer.resolution_elevation"
				property string configProp: "value"
				property var encode: function(v) { return Math.round(v) }
				property var decode: function(v) { return Number(v) }
				from: 4
				to: 128
				stepSize: 1
				implicitWidth: 210
				Component.onCompleted: demoDrawer.configManager.register(this)
				onValueChanged: demoDrawer.configManager.onControlChanged(this)
				value: 32
				ToolTip.visible: hovered
				ToolTip.text: "" + Math.round(value)
				visible: advancedSettings.checked
			}

			// colorize delay only makes sense for FFT beamformer
			Label { text: "Color"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: beamformerType.currentIndex === 0 }
			ComboBox {
				id: colorizeDelay
				property string configKey: "beamformer.colorize_delay"
				property string configProp: "currentIndex"
				property var encode: function(v) { return colorizeDelay.currentIndex === 1 }
				property var decode: function(v) { return v ? 1 : 0 }
				Component.onCompleted: demoDrawer.configManager.register(this)
				onCurrentIndexChanged: demoDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				model: ["Default", "Show Delay"]
				currentIndex: 0
				visible: beamformerType.currentIndex === 0
			}

			Label { text: "Max Delay"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: beamformerType.currentIndex === 0 && colorizeDelay.currentIndex === 1 }
			Slider {
				id: maxDelay
				property string configKey: "beamformer.max_delay"
				property string configProp: "value"
				from: 0.01
				to: 0.8
				stepSize: 0.01
				implicitWidth: 210
				Component.onCompleted: demoDrawer.configManager.register(this)
				onValueChanged: demoDrawer.configManager.onControlChanged(this)
				value: 0.2
				visible: beamformerType.currentIndex === 0 && colorizeDelay.currentIndex === 1
				ToolTip.visible: hovered
				ToolTip.text: "In samples. Color hue indicates relative delay up to this maximum. Current value: " + value.toFixed(2)
			}

			Label { Layout.columnSpan: 2; text: "Visualization"; color: "#9fb3c8" }
			Label { text: "Space"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: spaceMode
				property string configKey: "visualization.space"
				property string configProp: "currentIndex"
				property var encode: function(v) { return spaceMode.model[v] }
				property var decode: function(v) {
					let idx = spaceMode.model.indexOf(v)
					return idx >= 0 ? idx : 0
				}
				Component.onCompleted: demoDrawer.configManager.register(this)
				onCurrentIndexChanged: demoDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				model: ["Camera", "Beamspace"]
				currentIndex: 0
			}

			Label { text: "Overlay"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: overlayMode
				property string configKey: "visualization.overlay"
				property string configProp: "currentIndex"
				property var encode: function(v) { return overlayMode.model[v] }
				property var decode: function(v) {
					let idx = overlayMode.model.indexOf(v)
					return idx >= 0 ? idx : 0
				}
				Component.onCompleted: demoDrawer.configManager.register(this)
				onCurrentIndexChanged: demoDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				model: ["Default", "Power"]
				currentIndex: 0
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