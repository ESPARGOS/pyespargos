import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

GridLayout {
    id: root
    property var configManager
    columns: 2
    columnSpacing: 16
    rowSpacing: 8
    Layout.columnSpan: 2
    Layout.fillWidth: true
    Label { text: "Space"; color: "white" }
    ComboBox {
        objectName: "visualizationSpace"
        Layout.fillWidth: true
        property string configKey: "visualization.space"
        property string configProp: "currentValue"
        textRole: "text"
        valueRole: "value"
        model: [{ value: "camera", text: "Camera" }, { value: "beamspace", text: "Beamspace" }]
        Component.onCompleted: root.configManager.register(this)
        onCurrentValueChanged: root.configManager.onControlChanged(this)
    }

	Label { Layout.columnSpan: 2; text: "Camera"; color: "#9fb3c8" }
	Label { text: "Device"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.preferredWidth: 120 }
	ComboBox {
                Layout.preferredWidth: 180
                Layout.fillWidth: true
		id: cameraDevice
        displayText: currentText.replace(/^.*?: /, "")
		property string configKey: "camera.device"
		property string configProp: "currentIndex"
		property var encode: function(v) { return cameraDevice.model[v] }
		property var decode: function(v) { return Math.max(0, cameraDevice.model.indexOf(v)) }
		Component.onCompleted: root.configManager.register(this)
		onActivated: root.configManager.onControlChanged(this)
		function isUserActive() { return pressed || popup.visible }
		ToolTip.visible: hovered
        ToolTip.text: currentText
        model: WebCam.availableDevices
		implicitWidth: 180
	}

	Label { text: "Format"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.preferredWidth: 120 }
	ComboBox {
                Layout.preferredWidth: 180
                Layout.fillWidth: true
		id: cameraFormat
        font.pixelSize: 13
        displayText: currentText.replace(/ @ ([0-9]+)\.[0-9]+ FPS /, " · $1 fps ")
		property string configKey: "camera.format"
		property string configProp: "currentIndex"
		property var encode: function(v) { return cameraFormat.model[v] }
		property var decode: function(v) { return Math.max(0, cameraFormat.model.indexOf(v)) }
		Component.onCompleted: root.configManager.register(this)
		onActivated: root.configManager.onControlChanged(this)
		function isUserActive() { return pressed || popup.visible }
		ToolTip.visible: hovered
        ToolTip.text: currentText
        model: WebCam.availableFormats
		implicitWidth: 180

	}

	Label { text: "Flip camera"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.preferredWidth: 120 }
	Switch {
		property string configKey: "camera.flip_camera"
		property string configProp: "checked"
		Component.onCompleted: root.configManager.register(this)
		onCheckedChanged: root.configManager.onControlChanged(this)
		checked: false
	}
	Label { text: "Flip overlay"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.preferredWidth: 120 }
	Switch {
		property string configKey: "camera.flip_overlay"
		property string configProp: "checked"
		Component.onCompleted: root.configManager.register(this)
		onCheckedChanged: root.configManager.onControlChanged(this)
		checked: false
	}
	Label { text: "FOV Azi"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.preferredWidth: 120 }
	ValueSlider {
		property string configKey: "camera.fov_azimuth"
		property string configProp: "value"
		property var encode: function(v) { return Math.round(v) }
		property var decode: function(v) { return Number(v) }
		Component.onCompleted: root.configManager.register(this)
		onEdited: root.configManager.onControlChanged(this)
		from: 10; to: 179; stepSize: 1; value: 72; implicitWidth: 180
		ToolTip.visible: hovered
		valueText: Math.round(value) + "°"
		ToolTip.text: valueText
	}
	Label { text: "FOV Ele"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.preferredWidth: 120 }
	ValueSlider {
		property string configKey: "camera.fov_elevation"
		property string configProp: "value"
		property var encode: function(v) { return Math.round(v) }
		property var decode: function(v) { return Number(v) }
		Component.onCompleted: root.configManager.register(this)
		onEdited: root.configManager.onControlChanged(this)
		from: 10; to: 120; stepSize: 1; value: 41; implicitWidth: 180
		ToolTip.visible: hovered
		valueText: Math.round(value) + "°"
		ToolTip.text: valueText
	}
	Label { text: "Azi shift"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.preferredWidth: 120 }
	ValueSlider {
		property string configKey: "visualization.azimuth_correction"
		property string configProp: "value"
		property var encode: function(v) { return Math.round(v) }
		property var decode: function(v) { return Number(v) }
		Component.onCompleted: root.configManager.register(this)
		onEdited: root.configManager.onControlChanged(this)
		from: -90; to: 90; stepSize: 1; value: 0; implicitWidth: 180
		ToolTip.visible: hovered
		valueText: Math.round(value) + "°"
		ToolTip.text: valueText
	}
	Label { text: "Ele shift"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.preferredWidth: 120 }
	ValueSlider {
		property string configKey: "visualization.elevation_correction"
		property string configProp: "value"
		property var encode: function(v) { return Math.round(v) }
		property var decode: function(v) { return Number(v) }
		Component.onCompleted: root.configManager.register(this)
		onEdited: root.configManager.onControlChanged(this)
		from: -90; to: 90; stepSize: 1; value: 0; implicitWidth: 180
		ToolTip.visible: hovered
		valueText: Math.round(value) + "°"
		ToolTip.text: valueText
	}
}
