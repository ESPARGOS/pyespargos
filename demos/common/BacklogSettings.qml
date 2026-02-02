import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Controls.Material
import "." as Common


Item  {
	id: backlogSettings
	property Item insertBefore: null
	onInsertBeforeChanged: {
		if (!insertBefore) return

		let items = [
			backlogHeader,
			sizeLabel,
			sizeRow,
			preambleLabel,
			preambleCombo
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
		id: backlogConfigManager
		endpoint: backlogconfig
	}

	Label {
		id: backlogHeader
		Layout.columnSpan: 2
		text: "Backlog"
		color: "#9fb3c8"
	}

	Label {
		id: sizeLabel
		text: "Size"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}

	RowLayout {
		id: sizeRow
		spacing: 14
		Slider {
			id: backlogSizeSlider
			property string configKey: "size"
			property string configProp: "value"
			property var encode: function(v) { return Math.round(v) }
			property var decode: function(v) { return Math.max(1, Math.min(1024, parseInt(v||64))) }
			Component.onCompleted: backlogConfigManager.register(this)
			onValueChanged: backlogConfigManager.onControlChanged(this)
			from: 1; to: 1024; value: 64; stepSize: 1
			implicitWidth: 120
			function isUserActive() { return pressed }
		}
		Label { text: backlogSizeSlider.value; color: "#ffffff" }
	}

	Label {
		id: preambleLabel
		text: "Preamble"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}

	ComboBox {
		id: preambleCombo
		property string configKey: "preamble"
		property string configProp: "currentValue"

		Component.onCompleted: backlogConfigManager.register(this)
		onCurrentValueChanged: backlogConfigManager.onControlChanged(this)

		implicitWidth: 210

		// Different internal representation than displayed strings
		model: [
			{ value: "lltf", text: "L-LTF"},
			{ value: "ht20", text: "HT20"},
			{ value: "ht40", text: "HT40"}
		]
		textRole: "text"
		valueRole: "value"
		currentIndex: 0
	}
}