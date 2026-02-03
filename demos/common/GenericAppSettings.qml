import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Controls.Material
import "." as Common


Item  {
	id: genericAppSettings
	property Item insertBefore: null
	onInsertBeforeChanged: {
		if (!insertBefore) return

		let items = [
			genericAppSettingsHeader,
			preambleFieldLabel,
			preambleFieldCombo
		]

		let targetLayout = insertBefore.parent

		// Find index of insertBefore in its parent layout
		let insert_index = -1
		let childList = []
		for (let i = 0; i < targetLayout.children.length; i++) {
			childList.push(targetLayout.children[i])
			if (targetLayout.children[i] === insertBefore) {
				insert_index = i
			}
		}

		if (insert_index < 0) return

		// Collect items that should come after our inserted items
		let itemsAfter = childList.slice(insert_index)

		// Reparent items after anchor to null temporarily
		for (let i = 0; i < itemsAfter.length; i++) {
			itemsAfter[i].parent = null
		}

		// Add our items to the layout (they go at the end now)
		for (let i = 0; i < items.length; i++) {
			items[i].parent = targetLayout
		}

		// Re-add the items that should come after
		for (let i = 0; i < itemsAfter.length; i++) {
			itemsAfter[i].parent = targetLayout
		}
	}

	Common.ConfigManager {
		id: genericConfigManager
		endpoint: genericconfig
	}

	Component.onCompleted: genericConfigManager.fetchAndApply()

	Label {
		id: genericAppSettingsHeader
		Layout.columnSpan: 2;
		text: "Generic App Settings";
		color: "#9fb3c8"
	}

	Label {
		id: preambleFieldLabel
		text: "Preamble"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}

	ComboBox {
		id: preambleFieldCombo
		property string configKey: "preamble_format"
		property string configProp: "currentValue"

		Component.onCompleted: genericConfigManager.register(this)
		onCurrentValueChanged: genericConfigManager.onControlChanged(this)

		implicitWidth: 210

		// Different internal representation than displayed strings
		model: [
			{ value: "lltf", text: "L-LTF"},
			{ value: "ht20", text: "HT20"},
			{ value: "ht40", text: "HT40"}
		]
		textRole: "text"
		valueRole: "value"
		currentValue: "lltf"
	}
}