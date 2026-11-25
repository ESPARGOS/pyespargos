import "." as IQ
import "../../common" as Common
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
 *     IQ.IQSettings {
 *         controller: iqcontrol        // IQController context property
 *         Layout.columnSpan: 2
 *         Layout.fillWidth: true
 *     }
 */
GridLayout {
	id: iqsettings
	enabled: controller && !controller.referenceBusy && hasSnapshot
	columns: 2
	columnSpacing: 16
	rowSpacing: 10

	// IQController backend (context property provided by the application)
	property var controller: null

	property bool hasSnapshot: false
	property bool _loading: false
	property int editingTriggerField: -1
	property string settingsError: ""
	property var thresholdControl: null
	property var triggerDefaults: ({})
	property bool compact: false
	readonly property bool showAdvanced: !compact || advancedButton.checked
	property bool allowSignal: true
	property bool allowStreaming: false
	readonly property var bluetoothRates: [32000000, 16000000, 8000000, 4000000, 3200000, 1600000]
	readonly property var bluetoothFilters: [1200, 1500, 2100, 2900, 3800, 5200, 8300]
	readonly property bool bluetooth: streaming || capturePath.currentIndex === 1
	readonly property var streamRates: [16000, 32000, 64000, 80000, 100000]
	readonly property bool streaming: triggerModes[triggerCombo.currentIndex]?.mode === "downsample"

	// Named trigger fields, matching the controller HTTP API.
	property var triggerModes: [
		{ name: "Interval", mode: "interval", fields: [
			{k: "interval_chunks", l: "Interval chunks", v: "16384"},
			{k: "offset_chunks", l: "Offset chunks", v: "0"},
			{k: "burst_chunks", l: "Burst chunks", v: "4"}
		]},
		{ name: "Accumulate", mode: "accumulate", fields: [
			{k: "vector_chunks", l: "Vector chunks", v: "4"},
			{k: "interval_chunks", l: "Interval chunks", v: "32768"},
			{k: "offset_chunks", l: "Offset chunks", v: "0"}
		]},
		{ name: "Signal", mode: "signal", fields: [
			{k: "threshold", l: "Threshold", v: "64"},
			{k: "antennas", l: "Trigger antennas", v: "0,1,2,3,4,5,6,7"},
			{k: "holdoff_ms", l: "Holdoff ms", v: "10"},
			{k: "capture_chunks", l: "Capture chunks (56–112)", v: "56"},
			{k: "quiet_threshold", l: "Quiet threshold", v: "0"}
		]}
	].filter(entry => allowSignal || entry.mode !== "signal").concat(allowStreaming ? [{name: "Digital downsampling", mode: "downsample", fields: []}] : [])
	property var triggerValues: triggerModes[0].fields.map(d => d.v)

	function triggerModeIndex(mode) {
		for (var i = 0; i < triggerModes.length; ++i) {
			if (triggerModes[i].mode === mode)
				return i
		}
		return 0
	}

	function triggerConfig() {
		const selected = triggerModes[triggerCombo.currentIndex]
		const trigger = {mode: selected.mode}
		selected.fields.forEach(function(field, index) {
			const value = String(triggerValues[index]).trim()
			trigger[field.k] = field.k === "antennas"
				? (value === "" ? [] : value.split(",").map(v => Number(v.trim())))
				: Number(value)
		})
		if (streaming) trigger.sample_rate_hz = streamRates[streamRate.currentIndex]
		return trigger
	}

	function pathConfig() {
		if (bluetooth) {
			return {capture_path: "bluetooth",
					bluetooth_sample_rate_hz: streaming ? 1600000 : bluetoothRates[btRate.currentIndex],
					bluetooth_filter_khz: streaming ? 1200 : bluetoothFilters[btFilter.currentIndex]}
		}
		return {capture_path: "wifi", adc_decimation: decimationCombo.currentValue,
				adc_source_sel: 15, filter_bw_mhz: lpfSwitch.checked ? lpfBwSlider.value : 0}
	}

	function pushConfig(patch) {
		if (!_loading && controller)
			controller.queue_config_json(JSON.stringify(patch))
	}

	function loadFromController() {
		if (controller && !controller.referenceBusy) {
			settingsError = ""
			controller.request_config()
		}
	}

	function showConfig(snapshot) {
		if (!controller || controller.referenceBusy) return
		_loading = true
		try {
			const cfg = JSON.parse(snapshot)
			var rx = Array.isArray(cfg.receivers) && cfg.receivers.length > 0
				? cfg.receivers[0] : {}
			if (rx.rf_freq_hz && !freqField.edited)
				freqField.text = String(rx.rf_freq_hz / 1e6)
			capturePath.currentIndex = cfg.capture_path === "bluetooth" ? 1 : 0
			btRate.currentIndex = Math.max(0, bluetoothRates.indexOf(cfg.bluetooth_sample_rate_hz))
			btFilter.currentIndex = Math.max(0, bluetoothFilters.indexOf(cfg.bluetooth_filter_khz))
			if (cfg.adc_decimation !== undefined) {
				var di = decimationCombo.indexOfValue(cfg.adc_decimation)
				if (di >= 0) decimationCombo.currentIndex = di
			}
			if (cfg.filter_bw_mhz !== undefined) {
				lpfSwitch.checked = cfg.filter_bw_mhz > 0
				if (cfg.filter_bw_mhz > 0 && !lpfBwSlider.pressed)
					lpfBwSlider.value = Math.min(Math.max(cfg.filter_bw_mhz, lpfBwSlider.from), lpfBwSlider.to)
			}
			if (cfg.rf_switch !== undefined)
				rfSwitchCombo.value = cfg.rf_switch
			if (rx.gain_mode !== undefined)
				agcSwitch.checked = rx.gain_mode === "auto"
			if (rx.rx_gain !== undefined && !rxGainSlider.pressed)
				rxGainSlider.value = Math.min(Math.max(rx.rx_gain, rxGainSlider.from), rxGainSlider.to)
			if (cfg.trigger !== undefined) {
				const nextMode = triggerModeIndex(cfg.trigger.mode)
				if (nextMode !== triggerCombo.currentIndex) editingTriggerField = -1
				triggerCombo.currentIndex = nextMode
				if (cfg.trigger.mode === "downsample") streamRate.currentIndex = Math.max(0, streamRates.indexOf(cfg.trigger.sample_rate_hz))
				var defs = triggerModes[triggerCombo.currentIndex].fields
				var vals = []
				for (var i = 0; i < defs.length; ++i) {
					var w = cfg.trigger[defs[i].k]
					if (Array.isArray(w)) w = w.join(",")
					vals.push(i === editingTriggerField ? triggerValues[i] : String(w ?? defs[i].v))
				}
				triggerValues = vals
			}
			gainPhaseSwitch.checked = controller.gain_phase_compensation
			hasSnapshot = true
		} catch (e) {
			console.warn("IQSettings: could not load config: " + e)
		}
		_loading = false
	}

	Component.onCompleted: loadFromController()
	Connections {
		target: iqsettings.controller
		function onConfigFailed(message) { iqsettings.settingsError = message }
		function onConfigApplied(patch) { iqsettings.settingsError = "" }
		function onConfigSnapshot(snapshot) { iqsettings.showConfig(snapshot) }
		function onReferenceBusyChanged() {
			if (!iqsettings.controller.referenceBusy) iqsettings.loadFromController()
		}
	}

	Label { visible: !iqsettings.compact; Layout.columnSpan: 2; text: "IQ Capture"; color: "#9fb3c8" }

	Button {
		objectName: "syncCalibrationButton"
		Layout.columnSpan: 2
		Layout.fillWidth: true
		text: "Sync + Calibration"
		Material.background: !controller || controller.syncNeeded ? "#c53d3d" : "#227b3d"
		Material.foreground: "#ffffff"
		onClicked: if (controller) controller.sync_async()
		ToolTip.visible: hovered
		ToolTip.text: "Synchronize and calibrate all sensors using broadband REF at fixed gain and transmit power, verify the result, then restore receive settings."
	}

	Label {
		Layout.columnSpan: 2
		Layout.fillWidth: true
		text: iqsettings.settingsError || controller?.calibrationError || ""
		visible: text.length > 0 && !controller?.referenceBusy
		wrapMode: Text.Wrap
		color: "#ffb4ab"
	}

	Label { text: "Frequency (MHz)"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	TextField {
		id: freqField
		property bool edited: false
		onTextEdited: edited = true
		text: "2437"
		implicitWidth: 210
		validator: DoubleValidator { bottom: 2000; top: 7000 }
		onEditingFinished: {
			if (edited && acceptableInput) pushConfig({rf_freq_hz: Math.round(parseFloat(text) * 1e6)})
			edited = false
		}
	}

	Label { text: "RF input"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	Common.RFSwitchControl {
		id: rfSwitchCombo
		onActivated: pushConfig({rf_switch: value})
	}

	// Gain like the WiFi settings: AGC switch + manual RX gain slider (the
	// firmware's expert mode, gain_mode 2, is deliberately not exposed here;
	// a loaded expert config displays as manual).
	Label { text: "Automatic gain"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	Switch {
		id: agcSwitch
		checked: false
		onToggled: pushConfig({gain_mode: checked ? "auto" : "manual", rx_gain: rxGainSlider.value})
		ToolTip.visible: hovered
		ToolTip.text: "Hardware AGC. Off: all sensors use the fixed manual RX gain below (comparable amplitudes across the array)."
	}

	Label { text: "RX Gain"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	Common.GainSlider {
		id: rxGainSlider
		value: 60
		enabled: !agcSwitch.checked
		onEdited: pushConfig({rx_gain: value})
	}

	Label { text: "Trigger"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	ComboBox {
		id: triggerCombo
		implicitWidth: 210
		model: triggerModes.map(mode => mode.name)
		onActivated: {
			const selected = iqsettings.triggerModes[currentIndex]
			const defaults = iqsettings.triggerDefaults[selected.mode] ?? {}
			iqsettings.triggerValues = selected.fields.map(field => String(defaults[field.k] ?? field.v))
			// Digital downsampling requires the Bluetooth path and its matching rate.
			const patch = iqsettings.streaming ? iqsettings.pathConfig() : {}
			patch.trigger = iqsettings.triggerConfig()
			iqsettings.pushConfig(patch)
		}
		ToolTip.visible: hovered
		ToolTip.text: iqsettings.triggerModes[currentIndex]?.mode === "signal"
			? "Any selected sensor can trigger a capture for the whole board. Enter participating antenna IDs as a comma-separated list."
			: ""
	}

	CheckBox {
		Layout.columnSpan: 2
		visible: iqsettings.thresholdControl !== null && iqsettings.triggerModes[triggerCombo.currentIndex]?.mode === "signal"
		text: "Automatic trigger threshold"
		checked: iqsettings.thresholdControl?.autoThreshold ?? false
		onToggled: {
			iqsettings.thresholdControl.setAutoThreshold(checked)
			if (!checked) iqsettings.loadFromController()
		}
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
				visible: (modelData.k !== "threshold" || !iqsettings.thresholdControl?.autoThreshold)
						 && (iqsettings.showAdvanced || ["threshold", "interval_chunks", "vector_chunks"].includes(modelData.k))
				spacing: 0
				Label { text: modelData.l; font.pixelSize: 10; color: "#9fb3c8" }
				TextField {
					text: iqsettings.triggerValues[index] !== undefined
						? iqsettings.triggerValues[index] : modelData.v
					implicitWidth: 74
					onTextEdited: {
						iqsettings.triggerValues[index] = text
						iqsettings.editingTriggerField = index
					}
					onEditingFinished: {
						if (iqsettings.editingTriggerField === index) {
							iqsettings.editingTriggerField = -1
							iqsettings.pushConfig({trigger: iqsettings.triggerConfig()})
						}
					}
				}
			}
		}
	}
	ToolButton {
		id: advancedButton
		Layout.columnSpan: 2
		visible: iqsettings.compact
		checkable: true
		text: checked ? "Hide advanced settings" : "Show advanced settings"
	}

	GridLayout {
		Layout.columnSpan: 2
		Layout.fillWidth: true
		columns: 2
		columnSpacing: iqsettings.columnSpacing
		rowSpacing: iqsettings.rowSpacing
		visible: iqsettings.showAdvanced
		Label { text: "Capture path"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
		ComboBox {
			id: capturePath
			model: ["WiFi", "Bluetooth"]
			displayText: iqsettings.streaming ? "Bluetooth" : currentText
			enabled: !iqsettings.streaming
			implicitWidth: 210
			onActivated: pushConfig(iqsettings.pathConfig())
		}
		Label { text: iqsettings.streaming ? "Output rate" : "Sample rate"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
		ComboBox {
			id: decimationCombo
			visible: !iqsettings.bluetooth
			implicitWidth: 210
			textRole: "text"
			valueRole: "value"
			currentIndex: 4
			model: [
				{ text: "80 MSa/s", value: 1 }, { text: "40 MSa/s", value: 2 },
				{ text: "20 MSa/s", value: 4 }, { text: "10 MSa/s", value: 6 },
				{ text: "8 MSa/s", value: 8 }, { text: "4 MSa/s", value: 10 }
			]
			onActivated: pushConfig({adc_decimation: currentValue})
		}

		ComboBox {
			id: btRate
			visible: iqsettings.bluetooth && !iqsettings.streaming
			implicitWidth: 210
			model: iqsettings.bluetoothRates.map(hz => (hz / 1e6) + " MSa/s")
			onActivated: pushConfig({bluetooth_sample_rate_hz: bluetoothRates[currentIndex]})
		}
		ComboBox {
			id: streamRate
			visible: iqsettings.streaming
			implicitWidth: 210
			model: iqsettings.streamRates.map(hz => (hz / 1000) + " kSa/s")
			onActivated: pushConfig({trigger: triggerConfig()})
		}
		// Analog RX (WifiRX0) low-pass filter: off = open (widest, ~54 MHz);
		// on = continuous cap-DAC tuning over the calibrated 13..54 MHz range
		// (firmware interpolates to the 6-bit DAC, ~0.5 MHz effective steps).
		// Only effective in the 20 MHz CBW path.
		Label { visible: !iqsettings.bluetooth; text: "Enable LPF"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
		Switch {
			id: lpfSwitch
			visible: !iqsettings.bluetooth
			checked: true
			onToggled: pushConfig({filter_bw_mhz: checked ? lpfBwSlider.value : 0})
			ToolTip.visible: hovered
			ToolTip.text: "Analog RX low-pass filter ahead of the ADC (anti-aliasing / adjacent-channel rejection). Off = open (widest, ~54 MHz)."
		}

		Label { visible: !iqsettings.bluetooth; text: "Bandwidth"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
		RowLayout {
			visible: !iqsettings.bluetooth
			spacing: 14
			Slider {
				id: lpfBwSlider
				from: 13; to: 54; stepSize: 1; value: 40
				implicitWidth: 160
				enabled: lpfSwitch.checked
				onPressedChanged: if (!pressed) pushConfig({filter_bw_mhz: value})
			}
			Label {
				text: Math.round(lpfBwSlider.value) + " MHz"
				color: lpfSwitch.checked ? "#ffffff" : "#808080"
			}
		}

		Label { visible: iqsettings.bluetooth; text: "Analog bandwidth"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
		ComboBox {
			id: btFilter
			visible: iqsettings.bluetooth
			enabled: !iqsettings.streaming
			implicitWidth: 210
			displayText: iqsettings.streaming ? "1.2 MHz" : currentText
			model: iqsettings.bluetoothFilters.map(khz => (khz / 1000) + " MHz")
			onActivated: pushConfig({bluetooth_filter_khz: bluetoothFilters[currentIndex]})
		}

		Label { visible: !iqsettings.streaming; Layout.columnSpan: 2; text: "Corrections"; color: "#9fb3c8" }

		Label { visible: !iqsettings.streaming; text: "Gain phase"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
		Switch {
			id: gainPhaseSwitch
			visible: !iqsettings.streaming
			checked: true
			onToggled: if (controller) controller.set_gain_phase_compensation(checked)
			ToolTip.visible: hovered
			ToolTip.text: "Compensate deterministic phase jumps when automatic gain control crosses analog gain-element boundaries."
		}

	}

}
