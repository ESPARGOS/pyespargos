import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

/**
 * Reusable controls for the controller's reference CW tone generator (REFTX),
 * driven over the IQController tone slots. Lets a demo enable/disable a continuous
 * carrier on the reference distribution network and set its frequency and TX
 * amplitude — useful as a known signal for the phase-calibration path and for
 * manually probing the array response.
 *
 * The tone is exact and continuous up to ~2497 MHz (the WiFi-channel tone API);
 * above that a forced-VCO-cap path reaches ~2680 MHz. High-band tones free-run
 * (not XTAL-locked); placement uses hardcoded measured lookup tables — instant,
 * ~±2.4 MHz up to 2634 MHz, approximate (±10+ MHz thermal drift) above, where
 * the exact frequency is read off the waterfall. To receive the tone, set the
 * RF switch to "Reference".
 *
 * Usage inside a Common.AppDrawer / IQRxDrawer column:
 *
 *     Common.ReftxToneSettings { controller: iqcontrol; Layout.fillWidth: true }
 */
GridLayout {
	id: reftx
	columns: 2
	columnSpacing: 16
	rowSpacing: 10

	// IQController backend (context property provided by the application)
	property var controller: null
	property bool _loading: true
	readonly property bool highBand: freqField.text.length > 0 && parseFloat(freqField.text) > 2497

	function apply() {
		if (_loading || !controller)
			return
		// cbw arg is unused now (high band uses the deterministic cap driver)
		controller.tone_apply(enableSwitch.checked,
		                    parseFloat(freqField.text) || 2437,
		                    attenSlider.value, 0)
	}

	function loadFromController() {
		if (!controller)
			return
		_loading = true
		try {
			var t = JSON.parse(controller.tone_get_json())
			enableSwitch.checked = !!t.enable
			if (t.enable && t.freq_khz > 0)
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
		text: "Reference CW Tone"
		color: "#9fb3c8"
		topPadding: 8
	}

	Label { text: "Enable"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	Switch {
		id: enableSwitch
		checked: false
		onToggled: reftx.apply()
		ToolTip.visible: hovered
		ToolTip.text: "Transmit a continuous carrier on the reference distribution network. Set the RF switch to Reference to receive it. Suppresses reference packet TX while on."
	}

	Label { text: "Frequency (MHz)"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	TextField {
		id: freqField
		text: "2437"
		implicitWidth: 210
		enabled: enableSwitch.checked
		// locale "C": accept "2437.25" with a dot decimal separator regardless
		// of system locale (parseFloat in apply() only understands the dot)
		validator: DoubleValidator { bottom: 2397; top: 2680; decimals: 3; notation: DoubleValidator.StandardNotation; locale: "C" }
		onEditingFinished: reftx.apply()
		ToolTip.visible: hovered
		ToolTip.text: "2397–2497 MHz: exact, continuous (kHz resolution, e.g. 2437.25). 2497–2634 MHz: deterministic (hardcoded VCO-cap table, ~±2.4 MHz). 2634–2680 MHz: approximate (topmost VCO band drifts ±10+ MHz with temperature — read exact off the waterfall; nudging still moves it monotonically)."
	}

	Label { text: "TX attenuation"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
	RowLayout {
		spacing: 14
		Slider {
			id: attenSlider
			from: 0; to: 20; stepSize: 1; value: 6
			implicitWidth: 160
			enabled: enableSwitch.checked
			onPressedChanged: if (!pressed) reftx.apply()
		}
		Label {
			text: Math.round(attenSlider.value) + " dB"
			color: enableSwitch.checked ? "#ffffff" : "#808080"
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
