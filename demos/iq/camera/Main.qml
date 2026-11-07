pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../../common" as Common
import "../common" as IQ
import "../../common/camera" as Camera

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 1100
	minimumHeight: 700
	title: "Wireless Signal Camera"

	receiverDrawerComponent: Component {
		IQ.IQRxDrawer { controller: iqcontrol }
	}

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "IQ Camera"
			endpoint: appconfig
			implicitWidth: 460

			Label { Layout.columnSpan: 2; text: "Receiver"; color: "#9fb3c8" }
			Label { text: "Both feeds"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Switch {
				property string configKey: "receiver.both_feeds"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
				ToolTip.visible: hovered
				ToolTip.text: "Alternate antenna feeds to expose different polarizations and propagation paths. Reflections can still produce false directions."
			}
			Label { text: "Auto calibration"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Switch {
				property string configKey: "receiver.auto_calibration"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
				ToolTip.visible: hovered
				ToolTip.text: "Recalibrate whenever a changed capture setting invalidates the calibration."
			}
			Label { text: "Auto threshold"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Switch {
				property string configKey: "receiver.auto_threshold"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
				ToolTip.visible: hovered
				ToolTip.text: "In Signal mode, keep the trigger threshold just above the receiver noise."
			}

            Label { text: "Display"; color: "white"; Layout.alignment: Qt.AlignLeft }
            ComboBox {
                Layout.preferredWidth: 180
                Layout.fillWidth: true
                property string configKey: "visualization.display_mode"
                property string configProp: "currentValue"
                textRole: "text"; valueRole: "value"
                model: [{text: "Burst flashes", value: "flashes"}, {text: "Azimuth flashes", value: "azimuth"}, {text: "Spatial spectrum", value: "spectrum"}]
                Component.onCompleted: appDrawer.configManager.register(this)
                onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
            }
            Label { text: "Flash width"; color: "white"; Layout.alignment: Qt.AlignLeft }
            Camera.ValueSlider {
                property string configKey: "visualization.flash_width_deg"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                valueText: value.toFixed(1) + "°"
                from: 1; to: 12; stepSize: 0.5; value: 4; implicitWidth: 180
                ToolTip.visible: hovered
                ToolTip.text: value.toFixed(1) + "° marker width (not angular resolution)"
            }
			Label { Layout.columnSpan: 2; text: "Detection"; color: "#9fb3c8" }
			Label { text: "Threshold"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Camera.ValueSlider {
				property string configKey: "beamformer.threshold_db"
				property string configProp: "value"
				property var encode: function(v) { return Math.round(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				valueText: Math.round(value) + " dB"
                from: 3; to: 30; stepSize: 1; value: 6; implicitWidth: 180
				ToolTip.visible: hovered
				ToolTip.text: Math.round(value) + " dB above the noise floor"
			}
			Label { text: "Min. purity"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Camera.ValueSlider {
				property string configKey: "beamformer.min_purity"
				property string configProp: "value"
				property var encode: function(v) { return Number(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				valueText: Math.round(value * 100) + "%"
                from: 0; to: 0.95; stepSize: 0.05; value: 0.3; implicitWidth: 180
				ToolTip.visible: hovered
				ToolTip.text: "Require a plane-wave consistency score of " + Math.round(100 * value) + "%. Reflections can also score highly."
			}
			Label { text: "Min. bandwidth"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Camera.ValueSlider {
				property string configKey: "beamformer.min_bandwidth_mhz"
				property string configProp: "value"
				property var encode: function(v) { return Number(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				valueText: value.toFixed(1) + " MHz"
                from: 0; to: 20; stepSize: 0.5; value: 0; implicitWidth: 180
				ToolTip.visible: hovered
				ToolTip.text: "Ignore emissions narrower than " + value.toFixed(1) + " MHz"
			}
			Label { text: "Reject DC"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Switch {
				property string configKey: "beamformer.reject_dc"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
			}
			Label { text: "Band limit"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Switch {
				id: bandLimit
				property string configKey: "beamformer.band_limit"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: false
			}
			Label { text: "Band low [MHz]"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true; visible: bandLimit.checked }
			TextField {
				property string configKey: "beamformer.band_low_mhz"
				property string configProp: "text"
				property var encode: function(v) { return parseFloat(v) }
				property var decode: function(v) { return Number(v).toString() }
				Component.onCompleted: appDrawer.configManager.register(this)
				onEditingFinished: appDrawer.configManager.onControlChanged(this)
				text: "2400"; implicitWidth: 120; visible: bandLimit.checked
			}
			Label { text: "Band high [MHz]"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true; visible: bandLimit.checked }
			TextField {
				property string configKey: "beamformer.band_high_mhz"
				property string configProp: "text"
				property var encode: function(v) { return parseFloat(v) }
				property var decode: function(v) { return Number(v).toString() }
				Component.onCompleted: appDrawer.configManager.register(this)
				onEditingFinished: appDrawer.configManager.onControlChanged(this)
				text: "2500"; implicitWidth: 120; visible: bandLimit.checked
			}

			Label { Layout.columnSpan: 2; text: "View"; color: "#9fb3c8" }
			Label { text: "Method"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			ComboBox {
                Layout.preferredWidth: 180
                Layout.fillWidth: true
				property string configKey: "beamformer.type"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				textRole: "text"; valueRole: "value"; implicitWidth: 180
				model: [{ value: "MUSIC", text: "MUSIC" }, { value: "Bartlett", text: "Bartlett" }]
				currentValue: "MUSIC"
			}
			Label { text: "Persistence"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Camera.ValueSlider {
				property string configKey: "beamformer.persistence_s"
				property string configProp: "value"
				property var encode: function(v) { return Number(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 0.04; to: 2; stepSize: 0.02; value: 0.12; implicitWidth: 180
				ToolTip.visible: hovered
				valueText: Math.round(value * 1000) + " ms"
                ToolTip.text: valueText
			}
			Label { text: "Range"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Camera.ValueSlider {
				property string configKey: "visualization.dynamic_range_db"
				property string configProp: "value"
				property var encode: function(v) { return Math.round(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				valueText: Math.round(value) + " dB"
                from: 5; to: 40; stepSize: 1; value: 12; implicitWidth: 180
				ToolTip.visible: hovered
				ToolTip.text: Math.round(value) + " dB"
			}
			Label { text: "Markers"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Switch {
				property string configKey: "visualization.markers"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
				ToolTip.visible: hovered
				ToolTip.text: "Label the strongest emitters with azimuth / elevation relative to the array."
			}
            Label { text: "Outside-view markers"; color: "white"; Layout.alignment: Qt.AlignLeft }
            Switch {
                property string configKey: "visualization.edge_markers"
                property string configProp: "checked"
                Component.onCompleted: appDrawer.configManager.register(this)
                onCheckedChanged: appDrawer.configManager.onControlChanged(this)
                checked: true
            }
			Label { text: "Waterfall"; color: "white"; Layout.alignment: Qt.AlignLeft; Layout.fillWidth: true }
			Switch {
				property string configKey: "visualization.waterfall"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
			}

            Camera.CameraSettings { configManager: appDrawer.configManager }

		}
	}

	Camera.CameraOverlay {
        updateInterval: 25
		anchors.fill: parent
	}

    RowLayout {
        anchors.left: parent.left; anchors.top: parent.top; anchors.margins: 12
        Button { text: checked ? "Resume" : "Freeze"; checkable: true; onToggled: backend.setFrozen(checked) }
        Button { text: "Clear"; onClicked: backend.clearImage() }
        Label { text: backend.liveStats; color: "white"; style: Text.Outline; styleColor: "black" }
    }
    Label {
        visible: overlayModel.displayMode === "azimuth"
        anchors.top: parent.top; anchors.topMargin: 48
        anchors.horizontalCenter: parent.horizontalCenter
        text: "Azimuth only · elevation is not displayed"
        color: "white"; style: Text.Outline; styleColor: "black"
    }
	Rectangle {
		anchors.top: parent.top
		anchors.horizontalCenter: parent.horizontalCenter
		anchors.topMargin: 10
		width: captureStatus.implicitWidth + 24
		height: 30
		color: "#d0101418"
		border.color: overlayModel.captureStatus === "Calibrated" ? "#35c46a" : "#e0a329"
		radius: 5
		Text {
			id: captureStatus
			anchors.centerIn: parent
			text: overlayModel.captureStatus
			color: "white"
			font.bold: true
		}
	}

    Rectangle {
        anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.margins: 14; anchors.bottomMargin: 56
        width: info.implicitWidth + 24; height: 38; radius: 7; color: "#cc101a24"
        Text { id: info; anchors.centerIn: parent; text: overlayModel.spectrumStatus; color: "#dceef5"; font.pixelSize: 13 }
    }
	IQ.IQWaterfall {
        timeDescription: backend.waterfallTimeDescription
		visible: overlayModel.waterfallVisible
		model: overlayModel
		anchors.right: parent.right
		anchors.bottom: parent.bottom
		anchors.rightMargin: 14
		anchors.bottomMargin: 14
		width: Math.min(500, parent.width * 0.42)
		height: Math.min(230, parent.height * 0.30)
	}
}
