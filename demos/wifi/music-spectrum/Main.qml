import "../common" as WiFi
import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import QtQuick
import "../../common" as Common

Common.ESPARGOSApplication {
	id: window
	receiverDrawerComponent: Component { WiFi.WiFiTransceiverDrawer {} }
	visible: true
	minimumWidth: 800
	minimumHeight: 500

	color: "#11191e"
	title: "MUSIC Azimuth of Arrival Spectrum"

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig

			Label { text: "Signal components"; color: "#ffffff" }
			SpinBox {
				id: sourceCount
				objectName: "sourceCount"
				property string configKey: "sources"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				Layout.fillWidth: true
				from: 1
				to: backend.maximumSources
				value: 1
			}
			Label {
				Layout.columnSpan: 2
				Layout.maximumWidth: 290
				wrapMode: Text.WordWrap
				text: "Number of independent signal components assumed by MUSIC."
				color: "#9fb3c8"
			}

			WiFi.CSIAppSettings {
				id: genericAppSettings
				insertBefore: genericAppSettingsAnchor
			}

			Item {
				id: genericAppSettingsAnchor
				Layout.columnSpan: 2
				width: 0
				height: 0
				visible: false
			}

			WiFi.CSIBacklogSettings {
				id: backlogSettings
				insertBefore: backlogSettingsAnchor
			}

			Item {
				id: backlogSettingsAnchor
				Layout.columnSpan: 2
				width: 0
				height: 0
				visible: false
			}

			// Spacer
			Rectangle {
				id: endSpacer
				Layout.columnSpan: 2
				width: 1; height: 30
				color: "transparent"
			}
		}
	}

	ColumnLayout {
		height: parent.height
		width: parent.width

		ChartView {
			id: spatialSpectra
			Layout.alignment: Qt.AlignTop
			legend.visible: false
			legend.labelColor: "#e0e0e0"
			Layout.fillWidth: true
			Layout.fillHeight: true
			antialiasing: true
			backgroundColor: "#151f26"

			axes: [
				ValueAxis {
					id: musicSpectrumXAxis

					min: -90
					max: 90
					titleText: "<font color=\"#e0e0e0\">Broadside angle &Theta; [degrees]</font>"
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickInterval: 30
					tickType: ValueAxis.TicksDynamic
					labelsColor: "#e0e0e0"
				},
				ValueAxis {
					id: musicSpectrumYAxis

					min: -50
					max: 2
					titleText: "<font color=\"#e0e0e0\">MUSIC spectrum [dB relative to peak]</font>"
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickCount: 6
					labelFormat: "%.0f"
					labelsColor: "#e0e0e0"
				}
			]

			LineSeries {
				name: "MUSIC Spectrum"
				id: musicSpectrum
				axisX: musicSpectrumXAxis
				axisY: musicSpectrumYAxis

				Component.onCompleted : {
					for (const angle of backend.scanningAngles) {
						musicSpectrum.append(angle, 0)
					}
				}
			}

			Timer {
				interval: 1 / 60 * 1000
				running: true
				repeat: true
				onTriggered: {
					backend.updateSpatialSpectrum(musicSpectrum, musicSpectrumYAxis)
				}
			}
		}
	}
}
