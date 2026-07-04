import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Controls.Material
import "." as Common

// Shared radar TX settings panel. Binds to `appconfig` and drives the demo's
// RadarControlMixin (start_radar/stop_radar + live reconfigure). Insert into a
// demo's Common.AppDrawer GridLayout via `insertBefore: <anchorItem>`.
// RX data format is configured separately (see Common.GenericAppSettings).
Item {
	id: radarSettings
	property Item insertBefore: null
	property int controlWidth: 150

	onInsertBeforeChanged: {
		if (!insertBefore) return

		let items = [
			radarHeader,
			antennaLabel, antennaCombo,
			formatLabel, formatCombo,
			periodLabel, periodSpin,
			powerLabel, powerCombo,
			rfswitchLabel, rfswitchCombo,
			cfoLabel, cfoSwitch,
			gainLabel, gainSpin,
			radarButtonRow,
			radarStatusLabel
		]

		let targetLayout = insertBefore.parent
		let insert_index = -1
		let childList = []
		for (let i = 0; i < targetLayout.children.length; i++) {
			childList.push(targetLayout.children[i])
			if (targetLayout.children[i] === insertBefore) insert_index = i
		}
		if (insert_index < 0) return

		let itemsAfter = childList.slice(insert_index)
		for (let i = 0; i < itemsAfter.length; i++) itemsAfter[i].parent = null
		for (let i = 0; i < items.length; i++) items[i].parent = targetLayout
		for (let i = 0; i < itemsAfter.length; i++) itemsAfter[i].parent = targetLayout
	}

	Common.ConfigManager {
		id: radarConfigManager
		endpoint: appconfig
	}

	Component.onCompleted: radarConfigManager.fetchAndApply()

	Label {
		id: radarHeader
		Layout.columnSpan: 2
		text: "Radar Transmitter"
		color: "#9fb3c8"
	}

	Label {
		id: antennaLabel
		text: "TX Antenna"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}
	ComboBox {
		id: antennaCombo
		property string configKey: "tx_antenna"
		property string configProp: "currentValue"
		Component.onCompleted: radarConfigManager.register(this)
		onCurrentValueChanged: radarConfigManager.onControlChanged(this)
		implicitWidth: radarSettings.controlWidth
		// Pool-wide antennas ("All (TDM)" + one entry per antenna across every array)
		model: backend.txAntennas
		textRole: "text"
		valueRole: "value"
		currentValue: 3
	}

	Label {
		id: formatLabel
		text: "TX Format"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}
	ComboBox {
		id: formatCombo
		property string configKey: "tx_format"
		property string configProp: "currentValue"
		Component.onCompleted: radarConfigManager.register(this)
		onCurrentValueChanged: radarConfigManager.onControlChanged(this)
		implicitWidth: radarSettings.controlWidth
		model: [
			{ value: "lltf", text: "L-LTF" },
			{ value: "ht20", text: "HT20" },
			{ value: "ht40", text: "HT40" },
			{ value: "he20", text: "HE20" }
		]
		textRole: "text"
		valueRole: "value"
		currentValue: "lltf"
	}

	Label {
		id: periodLabel
		text: "Interval [ms]"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}
	SpinBox {
		id: periodSpin
		property string configKey: "period_ms"
		property string configProp: "value"
		Component.onCompleted: radarConfigManager.register(this)
		onValueChanged: radarConfigManager.onControlChanged(this)
		implicitWidth: radarSettings.controlWidth
		from: 1
		to: 100
		value: 5
		function isUserActive() { return activeFocus }
	}

	Label {
		id: powerLabel
		text: "TX Power"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}
	ComboBox {
		id: powerCombo
		property string configKey: "tx_power"
		property string configProp: "currentValue"
		Component.onCompleted: radarConfigManager.register(this)
		onCurrentValueChanged: radarConfigManager.onControlChanged(this)
		implicitWidth: radarSettings.controlWidth
		model: [
			{ value: 8, text: "2 dBm" },
			{ value: 20, text: "5 dBm" },
			{ value: 28, text: "7 dBm" },
			{ value: 34, text: "8.5 dBm" },
			{ value: 44, text: "11 dBm" },
			{ value: 52, text: "13 dBm" },
			{ value: 56, text: "14 dBm" },
			{ value: 60, text: "15 dBm" },
			{ value: 66, text: "16.5 dBm" },
			{ value: 72, text: "18 dBm" },
			{ value: 80, text: "20 dBm" }
		]
		textRole: "text"
		valueRole: "value"
		currentValue: 80
	}

	Label {
		id: rfswitchLabel
		text: "RF Switch"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}
	ComboBox {
		id: rfswitchCombo
		property string configKey: "rfswitch_state"
		property string configProp: "currentValue"
		Component.onCompleted: radarConfigManager.register(this)
		onCurrentValueChanged: radarConfigManager.onControlChanged(this)
		implicitWidth: radarSettings.controlWidth
		model: [
			{ value: 0, text: "Isolated" },
			{ value: 1, text: "Reference" },
			{ value: 2, text: "Antenna R" },
			{ value: 3, text: "Antenna L" },
			{ value: 4, text: "Random" }
		]
		textRole: "text"
		valueRole: "value"
		currentValue: 2
	}

	Label {
		id: cfoLabel
		text: "CFO comp."
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}
	Switch {
		id: cfoSwitch
		property string configKey: "cfo_compensation"
		property string configProp: "checked"
		Component.onCompleted: radarConfigManager.register(this)
		onCheckedChanged: radarConfigManager.onControlChanged(this)
		checked: false
	}

	Label {
		id: gainLabel
		text: "Gain cal. [s]"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}
	SpinBox {
		id: gainSpin
		property string configKey: "gain_calib_duration"
		property string configProp: "value"
		Component.onCompleted: radarConfigManager.register(this)
		onValueChanged: radarConfigManager.onControlChanged(this)
		implicitWidth: radarSettings.controlWidth
		from: 1
		to: 5
		value: 1
		function isUserActive() { return activeFocus }
	}

	RowLayout {
		id: radarButtonRow
		Layout.columnSpan: 2
		Layout.alignment: Qt.AlignCenter
		spacing: 10
		Button {
			text: "Start Radar"
			onClicked: backend.start_radar()
		}
		Button {
			text: "Stop Radar"
			onClicked: backend.stop_radar()
		}
	}

	Label {
		id: radarStatusLabel
		Layout.columnSpan: 2
		Layout.alignment: Qt.AlignHCenter
		text: `Status: ${backend.radar_status}`
		color: "#ffffff"
		font.pixelSize: 13
	}
}
