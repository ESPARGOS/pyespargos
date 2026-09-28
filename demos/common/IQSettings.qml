import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

/**
 * Reusable IQ-sampling capture controls for demo app drawers.
 *
 * Provides the common ESPARGOS IQ interface: center frequency, sample rate
 * (ADC decimation), analog RX filter bandwidth, RF switch, gain and trigger
 * configuration (Interval / Accumulate / Signal, including holdoff), plus the time-sync
 * and phase-calibration actions. Mode handling is not exposed: the
 * application enters IQ mode at startup and restores WiFi/CSI mode on exit.
 *
 * The controller is the source of truth: values are loaded from it on
 * completion and every change is pushed immediately via set_iq_control.
 *
 * Usage inside a Common.AppDrawer (2-column GridLayout):
 *
 *     Common.IQSettings {
 *         controller: iqcontrol        // IQController context property
 *         Layout.columnSpan: 2
 *         Layout.fillWidth: true
 *     }
 */
GridLayout {
	id: iqsettings
	columns: 2
	columnSpacing: 16
	rowSpacing: 10

	// IQController backend (context property provided by the application)
	property var controller: null

	property bool _loading: true

	// Trigger configuration words per UI mode. Firmware mode IDs are 0 for
	// interval, 3 for accumulation and 4 for array-wide wired-OR Signal.
	// Interval default: burst 4 / period 4096 = ~305 chunks/s per sensor at decim 1,
	// just inside the measured ~385 chunks/s SPI uplink share — beyond it, each
	// sensor drops a different random subset and index-matched (8/8) sets vanish.
	property var triggerModes: [
		{ name: "Interval", mode: 0, fields: [
			{l: "Period", v: "4096"}, {l: "Offset", v: "0"}, {l: "Burst", v: "4"}
		]},
		{ name: "Accumulate", mode: 3, fields: [
			{l: "Vector chunks", v: "4"}, {l: "Stream interval", v: "32768"},
			{l: "Offset", v: "0"}
		]},
		{ name: "Signal", mode: 4, fields: [
			{l: "Threshold", v: "64"}, {l: "Trigger sensors", v: "0xff"},
			{l: "Holdoff ms", v: "1"}, {l: "Capture chunks (56–112)", v: "56"}
		]}
	]
	property var triggerValues: triggerModes[0].fields.map(d => d.v)

	function triggerModeIndex(mode) {
		for (var i = 0; i < triggerModes.length; ++i) {
			if (triggerModes[i].mode === mode)
				return i
		}
		return 0
	}

	function parseTriggerValue(text) {
		var stringValue = String(text).trim()
		var value = stringValue.toLowerCase().startsWith("0x")
			? parseInt(stringValue.slice(2), 16) : parseInt(stringValue, 10)
		return isNaN(value) ? 0 : value
	}

	function collectConfig() {
		var triggerConfig = triggerValues.map(parseTriggerValue)
		var selectedTrigger = triggerModes[triggerCombo.currentIndex]
		if (selectedTrigger.mode === 3)
			triggerConfig.push(0) // reserved accumulation word
		if (selectedTrigger.mode === 4)
			triggerConfig[3] = Math.max(56, Math.min(112, triggerConfig[3]))
		return {
			"rf_freq_hz": Math.round(parseFloat(freqField.text) * 1e6),
			"adc_decimation": parseInt(decimationCombo.currentValue),
			"adc_source_sel": 15,
			"filter_bw_mhz": lpfSwitch.checked ? Math.round(lpfBwSlider.value) : 0,
			"gain_mode": agcSwitch.checked ? 0 : 1,
			"rx_gain": Math.round(rxGainSlider.value),
			"trigger_mode": selectedTrigger.mode,
			"trigger_config": triggerConfig,
			"rf_switch": rfSwitchCombo.currentIndex
		}
	}

	function pushConfig() {
		if (_loading || !controller)
			return
		controller.apply_config_json(JSON.stringify(collectConfig()))
	}

	function loadFromController() {
		if (!controller)
			return
		_loading = true
		try {
			var cfg = JSON.parse(controller.get_config_json())
			var rx = Array.isArray(cfg.receivers) && cfg.receivers.length > 0
				? cfg.receivers[0] : {}
			if (rx.rf_freq_hz)
				freqField.text = (rx.rf_freq_hz / 1e6).toFixed(0)
			if (cfg.adc_decimation !== undefined) {
				var di = decimationCombo.indexOfValue(cfg.adc_decimation)
				if (di >= 0) decimationCombo.currentIndex = di
			}
			if (cfg.filter_bw_mhz !== undefined) {
				lpfSwitch.checked = cfg.filter_bw_mhz > 0
				if (cfg.filter_bw_mhz > 0)
					lpfBwSlider.value = Math.min(Math.max(cfg.filter_bw_mhz, lpfBwSlider.from), lpfBwSlider.to)
			}
			if (cfg.rf_switch !== undefined)
				rfSwitchCombo.currentIndex = cfg.rf_switch
			if (rx.gain_mode !== undefined)
				agcSwitch.checked = rx.gain_mode === "auto"
			if (rx.rx_gain !== undefined)
				rxGainSlider.value = Math.min(Math.max(rx.rx_gain, rxGainSlider.from), rxGainSlider.to)
			if (cfg.trigger_mode !== undefined && Array.isArray(cfg.trigger_config)) {
				triggerCombo.currentIndex = triggerModeIndex(cfg.trigger_mode)
				var defs = triggerModes[triggerCombo.currentIndex].fields
				var vals = []
				for (var i = 0; i < defs.length; ++i) {
					var w = cfg.trigger_config[i]
					vals.push("" + (w !== undefined ? w : defs[i].v))
				}
				triggerValues = vals
			}
			gainPhaseSwitch.checked = controller.gain_phase_compensation
		} catch (e) {
			console.warn("IQSettings: could not load config: " + e)
		}
		_loading = false
	}

	Component.onCompleted: loadFromController()

	Label { Layout.columnSpan: 2; text: "IQ Capture"; color: "#9fb3c8" }

	Label { text: "Time sync"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	Button {
		text: "Sync"
		onClicked: if (controller) controller.sync_async()
		ToolTip.visible: hovered
		ToolTip.text: "Coarse array-wide time sync: anchors all sensors' IQ chunk grids to one WiFi reference packet (round-trips through WiFi mode, a few seconds). Runs automatically at application startup — re-run only to recover a lost grid alignment. Invalidates the fine calibration."
	}

	Label { text: "Calibration"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	Button {
		text: "Calibrate"
		onClicked: if (controller) controller.calibrate_auto_async()
		ToolTip.visible: hovered
		ToolTip.text: "Fine time/phase calibration vs sensor 0 on the EXISTING coarse sync: RF switch to Reference, quick CW sweep across the band (channel tones and/or forced-VCO-cap tones), complex-domain phase+delay fit, restore. A few seconds; the waterfall stays live. Valid until the next sync or retune."
	}

	Label { Layout.columnSpan: 2; text: "Corrections"; color: "#9fb3c8" }

	Label { text: "Gain phase"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	Switch {
		id: gainPhaseSwitch
		checked: true
		onToggled: if (controller) controller.set_gain_phase_compensation(checked)
		ToolTip.visible: hovered
		ToolTip.text: "Compensate deterministic phase jumps when automatic gain control crosses analog gain-element boundaries."
	}

	Label { text: "Center freq (MHz)"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	TextField {
		id: freqField
		text: "2437"
		implicitWidth: 210
		validator: DoubleValidator { bottom: 2000; top: 7000 }
		onEditingFinished: pushConfig()
	}

	Label { text: "Sample rate"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	ComboBox {
		id: decimationCombo
		implicitWidth: 210
		textRole: "text"
		valueRole: "value"
		currentIndex: 4
		model: [
			{ text: "80 MSa/s", value: 1 }, { text: "40 MSa/s", value: 2 },
			{ text: "20 MSa/s", value: 4 }, { text: "10 MSa/s", value: 6 },
			{ text: "8 MSa/s", value: 8 }, { text: "4 MSa/s", value: 10 }
		]
		onActivated: pushConfig()
	}

	// Analog RX (WifiRX0) low-pass filter: off = open (widest, ~54 MHz);
	// on = continuous cap-DAC tuning over the calibrated 13..54 MHz range
	// (firmware interpolates to the 6-bit DAC, ~0.5 MHz effective steps).
	// Only effective in the 20 MHz CBW path.
	Label { text: "Enable LPF"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	Switch {
		id: lpfSwitch
		checked: true
		onToggled: pushConfig()
		ToolTip.visible: hovered
		ToolTip.text: "Analog RX low-pass filter ahead of the ADC (anti-aliasing / adjacent-channel rejection). Off = open (widest, ~54 MHz)."
	}

	Label { text: "Bandwidth"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	RowLayout {
		spacing: 14
		Slider {
			id: lpfBwSlider
			from: 13; to: 54; stepSize: 1; value: 40
			implicitWidth: 160
			enabled: lpfSwitch.checked
			onPressedChanged: if (!pressed) pushConfig()
		}
		Label {
			text: Math.round(lpfBwSlider.value) + " MHz"
			color: lpfSwitch.checked ? "#ffffff" : "#808080"
		}
	}

	Label { text: "RF switch"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	ComboBox {
		id: rfSwitchCombo
		implicitWidth: 210
		model: ["Isolation", "Reference", "Antenna R", "Antenna L"]
		currentIndex: 2
		onActivated: pushConfig()
	}

	// Gain like the WiFi settings: AGC switch + manual RX gain slider (the
	// firmware's expert mode, gain_mode 2, is deliberately not exposed here;
	// a loaded expert config displays as manual).
	Label { text: "Automatic gain"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	Switch {
		id: agcSwitch
		checked: false
		onToggled: pushConfig()
		ToolTip.visible: hovered
		ToolTip.text: "Hardware AGC. Off: all sensors use the fixed manual RX gain below (comparable amplitudes across the array)."
	}

	Label { text: "RX Gain"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	RowLayout {
		spacing: 14
		Slider {
			id: rxGainSlider
			from: 0; to: 76; stepSize: 1; value: 60
			implicitWidth: 160
			enabled: !agcSwitch.checked
			onPressedChanged: if (!pressed) pushConfig()
		}
		Label {
			text: Math.round(rxGainSlider.value)
			color: agcSwitch.checked ? "#808080" : "#ffffff"
		}
	}

	Label { text: "Trigger"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	ComboBox {
		id: triggerCombo
		implicitWidth: 210
		model: triggerModes.map(mode => mode.name)
		onActivated: {
			iqsettings.triggerValues = iqsettings.triggerModes[currentIndex].fields.map(d => d.v)
			pushConfig()
		}
		ToolTip.visible: hovered
		ToolTip.text: currentIndex === 2
			? "Array-wide wired-OR detection. Any enabled sensor may trigger; every sensor then streams the same capture. A 56-chunk capture contains the complete bank with the threshold crossing; larger values include more of the following bank. Trigger sensors is an 8-bit mask (for example 0xff)."
			: ""
	}

	Flow {
		Layout.columnSpan: 2
		Layout.fillWidth: true
		spacing: 8
		Repeater {
			model: iqsettings.triggerModes[triggerCombo.currentIndex].fields
			ColumnLayout {
				required property int index
				required property var modelData
				spacing: 0
				Label { text: modelData.l; font.pixelSize: 10; color: "#9fb3c8" }
				TextField {
					text: iqsettings.triggerValues[index] !== undefined
						? iqsettings.triggerValues[index] : modelData.v
					implicitWidth: 74
					onTextEdited: iqsettings.triggerValues[index] = text
					onEditingFinished: pushConfig()
				}
			}
		}
	}
}
