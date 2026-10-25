import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

/* Manual CW/noise reference controls; synchronization always uses noise. */
GridLayout {
	id: reftx
	enabled: controller && !controller.referenceBusy
	columns: 2
	columnSpacing: 16
	rowSpacing: 10

	// IQController backend (context property provided by the application)
	property var controller: null
	property bool _loading: true
	readonly property bool highBand: modeSelect.currentIndex === 1 && parseFloat(freqField.text) > 2497

	function apply() {
		if (_loading || !controller)
			return
		if (modeSelect.currentIndex === 2 && parseFloat(freqField.text) > 2640)
			freqField.text = "2640"
		controller.reference_apply(["off", "cw", "iq"][modeSelect.currentIndex],
		                          parseFloat(freqField.text) || 2437, attenSlider.value)
	}

	function loadFromController() {
		if (!controller || controller.referenceBusy)
			return
		_loading = true
		try {
			var t = JSON.parse(controller.tone_get_json())
			modeSelect.currentIndex = Math.max(0, ["off", "cw", "iq"].indexOf(t.mode))
			if (t.backoff_qdb !== undefined && t.mode !== "off") attenSlider.value = t.backoff_qdb / 4
			if (t.freq_khz > 0)
				// kHz resolution, trailing zeros trimmed ("2437", "2437.25")
				freqField.text = Number((t.freq_khz / 1000).toFixed(3)).toString()
		} catch (e) {
			console.warn("ReftxToneSettings: could not load tone state: " + e)
		}
		_loading = false
	}

	Component.onCompleted: loadFromController()
	onControllerChanged: loadFromController()

	// The tone is also changed behind this GUI's back (sync and calibration
	// disable it); refresh the controls whenever the controller touches it.
	Connections {
		target: reftx.controller
		ignoreUnknownSignals: true
		function onToneChanged() { reftx.loadFromController() }
	}

	Label {
		Layout.columnSpan: 2
		text: "Reference Signal"
		color: "#9fb3c8"
		topPadding: 8
	}

	Label { text: "Signal"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	ComboBox {
		id: modeSelect
		objectName: "referenceModeSelector"
		model: ["Off", "CW", "Noise (36 MHz)"]
		onActivated: reftx.apply()
	}

	Label { text: "Frequency (MHz)"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	TextField {
		id: freqField
		text: "2437"
		implicitWidth: 210
		enabled: modeSelect.currentIndex !== 0
		// locale "C": accept "2437.25" with a dot decimal separator regardless
		// of system locale (parseFloat in apply() only understands the dot)
		validator: DoubleValidator { bottom: 2310; top: modeSelect.currentIndex === 2 ? 2640 : 2680; decimals: 3; notation: DoubleValidator.StandardNotation; locale: "C" }
		onEditingFinished: reftx.apply()
		ToolTip.visible: hovered
		ToolTip.text: modeSelect.currentIndex === 2 ? "Broadband reference noise, centered at 2310–2640 MHz." : "2310–2497 MHz: exact, continuous (kHz resolution, e.g. 2437.25). 2497–2634 MHz: deterministic (hardcoded VCO-cap table, ~±2.4 MHz). 2634–2680 MHz: approximate (topmost VCO band drifts ±10+ MHz with temperature — read exact off the waterfall; nudging still moves it monotonically)."
	}

	Label { text: "TX attenuation"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	RowLayout {
		spacing: 14
		Slider {
			id: attenSlider
			from: 0; to: 20; stepSize: 0.25; value: 7.5
			implicitWidth: 160
			enabled: modeSelect.currentIndex !== 0
			onPressedChanged: if (!pressed) reftx.apply()
		}
		Label {
			text: Number(attenSlider.value.toFixed(2)) + " dB"
			color: modeSelect.currentIndex !== 0 ? "#ffffff" : "#808080"
		}
	}

	// High-band hint: the tone is deterministic but free-running; exact placement
	// needs the target inside the sensors' capture band.
	Label {
		Layout.columnSpan: 2
		Layout.fillWidth: true
		visible: reftx.highBand
		wrapMode: Text.WordWrap
		font.pixelSize: 11
		color: "#9fb3c8"
		text: "High band: placed instantly from a hardcoded cap↔frequency table. ≤2634 MHz: ~±2.4 MHz. Above: approximate (drifts ±10+ MHz with temperature) — read exact off the waterfall."
	}
}
