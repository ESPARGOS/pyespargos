import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import QtQuick.Controls.Material

Item {
	id: configManager
	visible: false
	width: 0
	height: 0

	property var controls: []
	property var cache: ({})
	// Configurable backend endpoint (e.g., backend.pool, backend.demo, ...)
	// Must provide get_config_json() and set_config_json(jsonStr)
	property var endpoint: backend

    // Suppress callbacks while applying state from backend/program
    property bool _suppressChanges: false

	function register(ctrl) {
		if (!ctrl || !ctrl.configKey || !ctrl.configProp) return
		controls.push(ctrl)
		if (cache.hasOwnProperty(ctrl.configKey)) setControl(ctrl, cache[ctrl.configKey])
	}
	function setControl(ctrl, jsonVal) {
		// If the control exposes user-activity, don't stomp on it while user interacts
		try {
			if (ctrl && typeof ctrl.isUserActive === "function" && ctrl.isUserActive()) return
		} catch (e) { /* ignore */ }

		_suppressChanges = true
		try { ctrl[ctrl.configProp] = ctrl.decode ? ctrl.decode(jsonVal) : jsonVal }
		catch (e) { console.log("Config setControl error for", ctrl.configKey, e) }
		_suppressChanges = false
	}
	function _getConfigJson() {
		try {
			if (!endpoint || typeof endpoint.get_config_json !== "function") return ""
			return endpoint.get_config_json()
		} catch (e) {
			console.log("Config get_config_json failed:", e)
			return ""
		}
	}

	function _setConfigJson(jsonStr) {
		try {
			if (!endpoint || typeof endpoint.set_config_json !== "function") return
			endpoint.set_config_json(jsonStr)
		} catch (e) {
			console.log("Config set_config_json failed:", e)
		}
	}
	function onControlChanged(ctrl) {
		// If control update was triggered by backend, ignore it
		if (_suppressChanges) return
		if (!ctrl || !ctrl.configKey || !ctrl.configProp) return
		let raw = ctrl[ctrl.configProp]
		let val = ctrl.encode ? ctrl.encode(raw) : raw
		cache[ctrl.configKey] = val
		let delta = {}; delta[ctrl.configKey] = val
		_setConfigJson(JSON.stringify(delta))
	}
	function applyConfig(obj) {
		cache = obj || {}
		for (let i = 0; i < controls.length; ++i) {
			let c = controls[i]
			if (cache.hasOwnProperty(c.configKey)) setControl(c, cache[c.configKey])
		}
	}
	function fetchAndApply() {
		try {
			let jsonStr = _getConfigJson()
			if (jsonStr && jsonStr.length) applyConfig(JSON.parse(jsonStr))
		} catch (e) { console.log("Config fetch failed:", e) }
	}
	// Forward actions (e.g., "calibrate") to endpoint.action(name, payloadJson)
	function action(actionName) {
		try {
			if (!endpoint || typeof endpoint.action !== "function") {
				console.log("Config action ignored (no endpoint.action):", actionName)
				return false
			}

			endpoint.action(actionName)
			return true
		} catch (e) {
			console.log("Config action failed:", actionName, e)
			return false
		}
	}

	// Modal error dialog shown on request (e.g., from backend)
	Dialog {
		id: errorDialog
		modal: true
		focus: true
		closePolicy: Popup.CloseOnEscape
		title: "Error"

		property string messageText: ""

		// Match app-wide Material settings
		Material.theme: Material.Dark
		Material.primary: "#227b3d"
		Material.accent: "#ffffff"
		Material.roundedScale: Material.notRounded

		parent: Overlay.overlay

		// Size constraints
		width: Math.min(640, parent ? parent.width - 60 : 640)
		height: Math.min(240, parent ? parent.height - 60 : 240)

		x: Math.round((parent.width - width) / 2)
		y: Math.round((parent.height - height) / 2)

		standardButtons: Dialog.Ok

		contentItem: ColumnLayout {
			spacing: 12
			anchors.fill: parent
			anchors.margins: 16

			ScrollView {
				Layout.fillWidth: true
				clip: true
				ScrollBar.vertical.policy: ScrollBar.AsNeeded

				TextArea {
					id: errorTextArea
					text: errorDialog.messageText
					readOnly: true
					wrapMode: TextArea.Wrap
					selectByMouse: true
					color: "#ffffff"
					selectionColor: "#227b3d"
					selectedTextColor: "#ffffff"
					anchors.fill: parent
					background: Rectangle {
						color: "transparent"
					}
				}
			}
		}
	}

	function showError(title, message) {
		errorDialog.title = (title && title.length) ? title : "Error"
		errorDialog.messageText = (message && message.length) ? message : ""
		errorDialog.open()
	}

	Connections {
		target: endpoint
		ignoreUnknownSignals: true

		// Optional backend hook: emit endpoint.showError(title, message)
		function onShowError(title, message) {
			configManager.showError(title, message)
		}

		function onConfigChanged(jsonStr) {
			try {
				if (jsonStr && jsonStr.length) configManager.applyConfig(JSON.parse(jsonStr))
			} catch (e) {
				console.log("Config push-update failed:", e)
			}
		}
	}

	function isValidMacAddress(s) {
		if (s === undefined || s === null) return false
		let t = ("" + s).trim()
		// 6 hex bytes separated by ':'
		return /^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$/.test(t)
	}
}