import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Controls.Material
import "." as Common

Drawer {
	id: root

	property int headerHeight: 0

	// Match app-wide Material settings
	Material.theme: Material.Dark
	Material.primary: "#227b3d"
	Material.accent: "#227b3d"
	Material.roundedScale: Material.notRounded

	implicitHeight: parent ? parent.height - headerHeight : 0
	y: headerHeight
	implicitWidth: 350
	edge: Qt.LeftEdge
	dragMargin: 50
	modal: false

	background: Rectangle {
		radius: 0
		color: "#222a2f"
	}

	ScrollView {
		anchors.fill: parent
		clip: false
		ScrollBar.vertical.visible: true
		anchors.leftMargin: 20
		anchors.rightMargin: 0
		anchors.topMargin: 0
		anchors.bottomMargin: 0

		GridLayout {
			Layout.alignment: Qt.AlignTop
			Layout.margins: 12
			columns: 2
			columnSpacing: 16
			rowSpacing: 10
			anchors.topMargin: 20
			anchors.bottomMargin: 20
			anchors.rightMargin: 20

			Label {
				Layout.columnSpan: 2
				text: "Receiver Settings"
				font.pixelSize: 18
				color: "#ffffff"
				horizontalAlignment: Text.AlignHCenter
				topPadding: 20
			}

			// Section: Channel
			Label { Layout.columnSpan: 2; text: "Channel"; color: "#9fb3c8" }
			Label { text: "Channel"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: chanInput
				property string configKey: "channel"
				property string configProp: "currentIndex"
				// Note: Channel is in range 1-13, but index is in range 0-12
				property var encode: function(v) { return v + 1 }
				property var decode: function(v) { return Math.max(0, Math.min(12, parseInt(v||1)-1)) }
				Component.onCompleted: poolConfigManager.register(this)
				onCurrentIndexChanged: {
					calibButton.needCalibration = true
					poolConfigManager.onControlChanged(this)
				}
				implicitWidth: 180
				model: [ "1 (2.412 GHz)", "2 (2.417 GHz)", "3 (2.422 GHz)", "4 (2.427 GHz)", "5 (2.432 GHz)", "6 (2.437 GHz)", "7 (2.442 GHz)", "8 (2.447 GHz)", "9 (2.452 GHz)", "10 (2.457 GHz)", "11 (2.462 GHz)", "12 (2.467 GHz)", "13 (2.472 GHz)" ]
				currentIndex: 0
				function isUserActive() { return pressed || popup.visible }
			}

			Label { text: "Secondary"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: secChanInput
				property string configKey: "secondary_channel"
				property string configProp: "currentIndex"
				property var encode: function(v) { return v }
				property var decode: function(v) { return Math.max(0, Math.min(3, parseInt(v||0))) }
				Component.onCompleted: poolConfigManager.register(this)
				onCurrentIndexChanged: {
					calibButton.needCalibration = true
					poolConfigManager.onControlChanged(this)
				}
				implicitWidth: 180
				model: [ "None", "Above", "Below" ]
				currentIndex: 0
				function isUserActive() { return pressed || popup.visible }
			}

			// Section: Calibration
			Label { Layout.columnSpan: 2; text: "Calibration"; color: "#9fb3c8" }
			Button {
				id: calibButton
				Layout.columnSpan: 2;
				Layout.alignment: Qt.AlignCenter;
				text: "Trigger Calibration";
				onClicked: {
					poolConfigManager.action("calibrate")
					needCalibration = false
				}
				property bool needCalibration: false

				// Button should have red border when calibration is needed
				Material.foreground: needCalibration ? "#ff4d4d" : "white"
			}

			// Calibration settings are still TODO
			/*
			Label { text: "Per Board"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: perBoardCalibSwitch
				property string configKey: "calibration.per_board"
				property string configProp: "checked"
				property var encode: function(v) { return v ? 1 : 0 }
				property var decode: function(v) { return !!v }
				Component.onCompleted: poolConfigManager.register(this)
				onCheckedChanged: poolConfigManager.onControlChanged(this)
				checked: false
			}
			Label { text: "Show Raw"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: showCalibSwitch
				property string configKey: "calibration.show_csi"
				property string configProp: "checked"
				property var encode: function(v) { return v ? 1 : 0 }
				property var decode: function(v) { return !!v }
				Component.onCompleted: poolConfigManager.register(this)
				onCheckedChanged: poolConfigManager.onControlChanged(this)
				checked: false
			}*/

			// Section: Signal Path
			Label { Layout.columnSpan: 2; text: "Signal Path / Format"; color: "#9fb3c8" }
			Label { text: "RF Switch"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: rfSwitchInput
				property string configKey: "rf_switch"
				property string configProp: "currentIndex"
				property var encode: function(v) { return v }
				property var decode: function(v) { return Math.max(0, Math.min(4, parseInt(v||0))) }
				Component.onCompleted: poolConfigManager.register(this)
				onCurrentIndexChanged: poolConfigManager.onControlChanged(this)
				implicitWidth: 180
				model: [ "Isolated", "Reference", "45° Right", "45° Left", "Random" ]
				currentIndex: 0
				function isUserActive() { return pressed || popup.visible }
			}

			Label { text: "Force L-LTF"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: forceAcquireLLTFSwitch
				property string configKey: "acquire_lltf_force"
				property string configProp: "checked"
				property var encode: function(v) { return v ? 1 : 0 }
				property var decode: function(v) { return !!v }
				Component.onCompleted: poolConfigManager.register(this)
				onCheckedChanged: poolConfigManager.onControlChanged(this)
				checked: false
			}

			// Section: Gain/AGC
			Label { Layout.columnSpan: 2; text: "RX Gain"; color: "#9fb3c8" }
			Label { text: "Automatic"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: rxGainSwitch
				property string configKey: "rx_gain.automatic"
				property string configProp: "checked"
				property var encode: function(v) { return v ? 1 : 0 }
				property var decode: function(v) { return !!v }
				Component.onCompleted: poolConfigManager.register(this)
				onCheckedChanged: poolConfigManager.onControlChanged(this)
				checked: true
			}

			Label { text: "Value"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			RowLayout {
				spacing: 14
				Slider {
					id: lnaGainSlider
					property string configKey: "rx_gain.value"
					property string configProp: "value"
					property var encode: function(v) { return Math.round(v) }
					property var decode: function(v) { return Math.max(0, Math.min(64, parseInt(v||32))) }
					Component.onCompleted: poolConfigManager.register(this)
					onValueChanged: poolConfigManager.onControlChanged(this)
					from: 0; to: 64; value: 32; stepSize: 1
					implicitWidth: 120
					enabled: !rxGainSwitch.checked
					function isUserActive() { return pressed }
				}
				Label { text: lnaGainSlider.value; color: "#ffffff" }
			}

			Label { Layout.columnSpan: 2; text: "FFT Gain"; color: "#9fb3c8" }
			Label { text: "Automatic"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: fftGainSwitch
				property string configKey: "fft_gain.automatic"
				property string configProp: "checked"
				property var encode: function(v) { return v ? 1 : 0 }
				property var decode: function(v) { return !!v }
				Component.onCompleted: poolConfigManager.register(this)
				onCheckedChanged: poolConfigManager.onControlChanged(this)
				checked: true
			}
			Label { text: "Value"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			RowLayout {
				spacing: 14
				Slider {
					id: fftGainSlider
					property string configKey: "fft_gain.value"
					property string configProp: "value"
					property var encode: function(v) { return Math.round(v) }
					property var decode: function(v) { return Math.max(0, Math.min(64, parseInt(v||32))) }
					Component.onCompleted: poolConfigManager.register(this)
					onValueChanged: poolConfigManager.onControlChanged(this)
					from: 0; to: 64; value: 32; stepSize: 1
					implicitWidth: 120
					enabled: !fftGainSwitch.checked
					function isUserActive() { return pressed }
				}
				Label { text: fftGainSlider.value; color: "#ffffff" }
			}

			// Section MAC filter
			Label { Layout.columnSpan: 2; text: "MAC Filter"; color: "#9fb3c8" }
			Label { text: "Enable Filter"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: macFilterSwitch
				property string configKey: "mac_filter.enable"
				property string configProp: "checked"
				property var encode: function(v) { return !!v }
				property var decode: function(v) { return !!v }
				Component.onCompleted: poolConfigManager.register(this)
				onCheckedChanged: poolConfigManager.onControlChanged(this)
				checked: false

				// Only enable MAC when address is valid
				enabled: macAddrInput.isValidMac
			}

			Label { text: "MAC Address"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			TextField {
				id: macAddrInput
				property string configKey: "mac_filter.mac_address"
				property string configProp: "text"
				property var encode: function(v) { return v.toString() }
				property var decode: function(v) { return v.toString() }
				property bool isValidMac: false

				Component.onCompleted: poolConfigManager.register(this)

				// Validate while editing
				onTextChanged: {
					isValidMac = poolConfigManager.isValidMacAddress(text)
					if (isValidMac) {
						poolConfigManager.onControlChanged(this)
					} else {
						macFilterSwitch.checked = false
					}
				}

				color: isValidMac ? "#ffffff" : "#ff4d4d"

				Material.accent: isValidMac ? "#227b3d" : "#ff4d4d"

				implicitWidth: 180
				placeholderText: "e.g., 12:34:56:78:9A:BC"
				function isUserActive() { return activeFocus }
			}

			// Spacer
			Rectangle {
				Layout.columnSpan: 2
				width: 1; height: 10
				color: "transparent"
			}

			Button {
				Layout.columnSpan: 2
				Layout.alignment: Qt.AlignCenter
				text: "Reload from Board"
				onClicked: poolConfigManager.action("reload_config")
			}

			Button {
				Layout.columnSpan: 2
				Layout.alignment: Qt.AlignCenter
				text: "Reset to Defaults"
				onClicked: {
					poolConfigManager.action("reset_config")
					// Note: UI will be updated via appChangedConfig connection
				}
			}

			// Spacer
			Rectangle {
				Layout.columnSpan: 2
				width: 1; height: 30
				color: "transparent"
			}
		}
	}

	Common.ConfigManager {
		id: poolConfigManager
		endpoint: poolconfig
	}

	Component.onCompleted: poolConfigManager.fetchAndApply()
}
