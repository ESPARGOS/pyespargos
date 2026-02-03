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
			lltfEnableLabel,
			lltfEnableSwitch,
			ht20EnableLabel,
			ht20EnableSwitch,
			ht40EnableLabel,
			ht40EnableSwitch
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

	Component.onCompleted: backlogConfigManager.fetchAndApply()

	Label {
		id: backlogHeader
		Layout.columnSpan: 2
		text: "CSI Backlog"
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
			from: 1; to: 128; value: 16; stepSize: 1
			implicitWidth: 120
			function isUserActive() { return pressed }
		}
		Label { text: backlogSizeSlider.value; color: "#ffffff" }
	}

	Label {
		id: lltfEnableLabel
		text: "Store L-LTF"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}

	Switch {
		id: lltfEnableSwitch
		property string configKey: "fields.lltf"
		property string configProp: "checked"

		Component.onCompleted: backlogConfigManager.register(this)
		onCheckedChanged: backlogConfigManager.onControlChanged(this)

		checked: true
	}

	Label {
		id: ht20EnableLabel
		text: "Store HT20"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}

	Switch {
		id: ht20EnableSwitch
		property string configKey: "fields.ht20"
		property string configProp: "checked"

		Component.onCompleted: backlogConfigManager.register(this)
		onCheckedChanged: backlogConfigManager.onControlChanged(this)

		checked: false
	}

	Label {
		id: ht40EnableLabel
		text: "Store HT40"
		color: "#ffffff"
		horizontalAlignment: Text.AlignRight
		Layout.alignment: Qt.AlignRight
		Layout.fillWidth: true
	}

	Switch {
		id: ht40EnableSwitch
		property string configKey: "fields.ht40"
		property string configProp: "checked"

		Component.onCompleted: backlogConfigManager.register(this)
		onCheckedChanged: backlogConfigManager.onControlChanged(this)

		checked: false
	}
}