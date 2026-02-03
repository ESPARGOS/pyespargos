import "."

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Controls.Material
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 800
	minimumHeight: 500

	color: "#333333"
	title: "ESPARGOS Camera Overlay Demo"

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "App Settings"
			endpoint: appconfig

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
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentIndexChanged: appDrawer.configManager.onControlChanged(this)
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
					appDrawer.configManager.register(this)

					// Populate formats for initial device
					cameraFormat.model = WebCam.availableFormats
				}
				onCurrentIndexChanged: appDrawer.configManager.onControlChanged(this)
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
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 80
				checked: false
			}

			Label { Layout.columnSpan: 2; text: "Receiver"; color: "#9fb3c8" }
			Label { text: "MAC List"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: macListEnabled
				property string configKey: "receiver.mac_list_enabled"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
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
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
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
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
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
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: {
					// colorize_delay only makes sense for FFT beamformer, set to false when changing away
					if (beamformerType.currentValue !== "FFT") {
						colorizeDelay.currentIndex = 0
					}
					appDrawer.configManager.onControlChanged(this)
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
				currentValue: "FFT"
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
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
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
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
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
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentIndexChanged: appDrawer.configManager.onControlChanged(this)
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
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				value: 0.2
				visible: beamformerType.currentIndex === 0 && colorizeDelay.currentIndex === 1
				ToolTip.visible: hovered
				ToolTip.text: "In samples. Color hue indicates relative delay up to this maximum. Current value: " + value.toFixed(2)
			}

			Label { text: "Timeout"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Slider {
				id: maxAge
				property string configKey: "beamformer.max_age"
				property string configProp: "value"
				property var encode: function(v) { return v }
				property var decode: function(v) { return Number(v) }
				from: 0.0
				to: 2.0
				stepSize: 0.05
				implicitWidth: 210
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				value: 0.0
				ToolTip.visible: hovered
				ToolTip.text: value === 0.0 ? "Disabled (use all data)" : "Use only CSI newer than " + value.toFixed(1) + " s"
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
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentIndexChanged: appDrawer.configManager.onControlChanged(this)
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
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentIndexChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 210
				model: ["Default", "Power"]
				currentIndex: 0
			}

			Label {
				Layout.columnSpan: 2
				text: "Exposure Settings"
				color: "#9fb3c8"
			}

			Label { text: "Manual"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: manualExposure
				property string configKey: "visualization.manual_exposure"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 80
				checked: false
			}

			Label { text: "Brightness"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: manualExposure.checked }
			Slider {
				id: exposureSlider
				property string configKey: "visualization.exposure"
				property string configProp: "value"
				property var encode: function(v) { return v }
				property var decode: function(v) { return Number(v) }
				from: 0.0
				to: 1.0
				stepSize: 0.01
				implicitWidth: 210
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				value: 0.5
				visible: manualExposure.checked
				ToolTip.visible: hovered
				ToolTip.text: "Exposure: " + (value * 100).toFixed(0) + "%"
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
				id: backlogSettingsAnchor;
				Layout.columnSpan: 2;
				width: 0;
				height: 0;
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

	CameraOverlay {
		anchors.fill: parent
	}
}