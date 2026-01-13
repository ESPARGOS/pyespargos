import QtQuick

QtObject {
	id: configManager
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
}