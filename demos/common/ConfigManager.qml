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
	// Must provide getConfigFromUI() and setConfigFromUI(jsonStr)
	property var endpoint: backend

    // Suppress callbacks while applying state from backend/program
    property bool _suppressChanges: false

	function _splitPath(key) {
		if (key === undefined || key === null) return []
		let parts = ("" + key).split(".")
		let out = []
		for (let i = 0; i < parts.length; ++i) {
			if (parts[i].length) out.push(parts[i])
		}
		return out
	}

	function _getPath(obj, key) {
		let parts = _splitPath(key)
		let cur = obj
		for (let i = 0; i < parts.length; ++i) {
			if (!cur || typeof cur !== "object" || !cur.hasOwnProperty(parts[i])) return undefined
			cur = cur[parts[i]]
		}
		return cur
	}

	function _hasPath(obj, key) {
		let parts = _splitPath(key)
		let cur = obj
		for (let i = 0; i < parts.length; ++i) {
			if (!cur || typeof cur !== "object" || !cur.hasOwnProperty(parts[i])) return false
			cur = cur[parts[i]]
		}
		return true
	}

	function _setPath(obj, key, value) {
		let parts = _splitPath(key)
		if (parts.length === 0) return
		let cur = obj
		for (let i = 0; i < parts.length - 1; ++i) {
			if (!cur.hasOwnProperty(parts[i]) || typeof cur[parts[i]] !== "object") {
				cur[parts[i]] = {}
			}
			cur = cur[parts[i]]
		}
		cur[parts[parts.length - 1]] = value
	}

	function register(ctrl) {
		if (!ctrl || !ctrl.configKey || !ctrl.configProp) return
		controls.push(ctrl)
		if (_hasPath(cache, ctrl.configKey)) setControl(ctrl, _getPath(cache, ctrl.configKey))
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
			if (!endpoint || typeof endpoint.getConfigFromUI !== "function") return ""
			return endpoint.getConfigFromUI()
		} catch (e) {
			console.log("Config getConfigFromUI failed:", e)
			return ""
		}
	}

	function _setConfigJson(jsonStr) {
		try {
			if (!endpoint || typeof endpoint.setConfigFromUI !== "function") return
			endpoint.setConfigFromUI(jsonStr)
		} catch (e) {
			console.log("Config setConfigFromUI failed:", e)
		}
	}
	function onControlChanged(ctrl) {
		// If control update was triggered by backend, ignore it
		if (_suppressChanges) return
		if (!ctrl || !ctrl.configKey || !ctrl.configProp) return
		let raw = ctrl[ctrl.configProp]
		let val = ctrl.encode ? ctrl.encode(raw) : raw
		_setPath(cache, ctrl.configKey, val)
		let delta = {}
		_setPath(delta, ctrl.configKey, val)
		_setConfigJson(JSON.stringify(delta))
	}
	function applyConfig(obj) {
		cache = obj || {}
		for (let i = 0; i < controls.length; ++i) {
			let c = controls[i]
			if (_hasPath(cache, c.configKey)) setControl(c, _getPath(cache, c.configKey))
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

		function onUpdateUIState(jsonStr) {
			try {
				if (jsonStr === undefined || jsonStr === null) return
				if (typeof jsonStr === "string") {
					if (jsonStr.length) configManager.applyConfig(JSON.parse(jsonStr))
				} else if (typeof jsonStr === "object") {
					configManager.applyConfig(jsonStr)
				}
				if (endpoint && typeof endpoint.updateUIStateHandled === "function") {
					endpoint.updateUIStateHandled()
				}
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