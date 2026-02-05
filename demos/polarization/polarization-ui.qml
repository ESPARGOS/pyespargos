import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import QtQuick
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 800
	minimumHeight: 500

	color: "#11191e"
	title: "Polarization"

	// Tab20 color cycle reordered: https://github.com/matplotlib/matplotlib/blob/main/lib/matplotlib/_cm.py#L1293
	property var colorCycle: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5", "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5"]
	property color textColor: "#DDDDDD"

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig

			Label { text: "Crosspol Fix"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: simpleJonesSwitch
				property string configKey: "crosspol_compensation"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 80
				checked: false
			}

			Label { text: "Show Mean"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: showMeanSwitch
				property string configKey: "show_mean"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 80
				checked: false
			}

			Common.GenericAppSettings {
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

			Common.BacklogSettings {
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

	Rectangle {
		anchors.fill: parent
		anchors.margins: 10
		color: "#151f26"
		opacity: 1

		ColumnLayout {
			anchors.fill: parent
			Layout.alignment: Qt.AlignCenter

			RowLayout {
				Layout.alignment: Qt.AlignCenter
				Layout.fillWidth: true
				Layout.fillHeight: true
				spacing: 20

				// Combined constellation diagram for L and R antenna feeds
				Item {
					id: chartFeedsContainer
					Layout.fillWidth: true
					Layout.fillHeight: true
					Layout.minimumWidth: 300
					Layout.minimumHeight: 300

					ChartView {
						id: chartFeeds
						title: "Antenna Feeds (L/R)"
						titleColor: textColor
						width: Math.min(parent.width, parent.height)
						height: Math.min(parent.width, parent.height)
						anchors.centerIn: parent
						antialiasing: true
						backgroundColor: "#1a252e"
						legend.visible: true
						legend.labelColor: textColor
						legend.alignment: Qt.AlignBottom

						ValueAxis {
							id: axisXFeeds
							min: -1.5
							max: 1.5
							titleText: "<font color=\"#e0e0e0\">I (In-Phase)</font>"
							titleFont.pixelSize: 12
							labelsColor: textColor
							titleVisible: true
							gridLineColor: "#3a4a5a"
							minorGridLineColor: "#2a3a4a"
							color: textColor
						}

						ValueAxis {
							id: axisYFeeds
							min: -1.5
							max: 1.5
							titleText: "<font color=\"#e0e0e0\">Q (Quadrature)</font>"
							titleFont.pixelSize: 12
							labelsColor: textColor
							titleVisible: true
							gridLineColor: "#3a4a5a"
							minorGridLineColor: "#2a3a4a"
							color: textColor
						}

						ScatterSeries {
							id: scatterL
							name: "L Feed"
							axisX: axisXFeeds
							axisY: axisYFeeds
							markerSize: 10
							color: "#1f77b4"
							borderColor: "#aec7e8"
							borderWidth: 1
						}

						ScatterSeries {
							id: scatterR
							name: "R Feed"
							axisX: axisXFeeds
							axisY: axisYFeeds
							markerSize: 10
							color: "#ff7f0e"
							borderColor: "#ffbb78"
							borderWidth: 1
						}
					}
				}

				// Combined constellation diagram for H and V (linear polarization)
				Item {
					id: chartLinearContainer
					Layout.fillWidth: true
					Layout.fillHeight: true
					Layout.minimumWidth: 300
					Layout.minimumHeight: 300

					ChartView {
						id: chartLinear
						title: "Linear Polarization (H/V)"
						titleColor: textColor
						width: Math.min(parent.width, parent.height)
						height: Math.min(parent.width, parent.height)
						anchors.centerIn: parent
						antialiasing: true
						backgroundColor: "#1a252e"
						legend.visible: true
						legend.labelColor: textColor
						legend.alignment: Qt.AlignBottom

						ValueAxis {
							id: axisXLinear
							min: -1.5
							max: 1.5
							titleText: "<font color=\"#e0e0e0\">I (In-Phase)</font>"
							titleFont.pixelSize: 12
							labelsColor: textColor
							titleVisible: true
							gridLineColor: "#3a4a5a"
							minorGridLineColor: "#2a3a4a"
							color: textColor
						}

						ValueAxis {
							id: axisYLinear
							min: -1.5
							max: 1.5
							titleText: "<font color=\"#e0e0e0\">Q (Quadrature)</font>"
							titleFont.pixelSize: 12
							labelsColor: textColor
							titleVisible: true
							gridLineColor: "#3a4a5a"
							minorGridLineColor: "#2a3a4a"
							color: textColor
						}

						ScatterSeries {
							id: scatterH
							name: "H Pol"
							axisX: axisXLinear
							axisY: axisYLinear
							markerSize: 10
							color: "#2ca02c"
							borderColor: "#98df8a"
							borderWidth: 1
						}

						ScatterSeries {
							id: scatterV
							name: "V Pol"
							axisX: axisXLinear
							axisY: axisYLinear
							markerSize: 10
							color: "#d62728"
							borderColor: "#ff9896"
							borderWidth: 1
						}
					}
				}
			}

			Timer {
				interval: 1 / 30 * 1000
				running: true
				repeat: true
				onTriggered: {
					backend.update()
				}
			}

			Connections {
				target: backend

				function onUpdateConstellation(feedLPoints, feedRPoints, linearHPoints, linearVPoints, axisScale) {
					// Update all axes with the same scale
					axisXFeeds.min = -axisScale
					axisXFeeds.max = axisScale
					axisYFeeds.min = -axisScale
					axisYFeeds.max = axisScale
					axisXLinear.min = -axisScale
					axisXLinear.max = axisScale
					axisYLinear.min = -axisScale
					axisYLinear.max = axisScale

					// Update L feed constellation points
					scatterL.clear()
					for (var i = 0; i < feedLPoints.length; i++) {
						scatterL.append(feedLPoints[i][0], feedLPoints[i][1])
					}

					// Update R feed constellation points
					scatterR.clear()
					for (var j = 0; j < feedRPoints.length; j++) {
						scatterR.append(feedRPoints[j][0], feedRPoints[j][1])
					}

					// Update H polarization constellation points
					scatterH.clear()
					for (var k = 0; k < linearHPoints.length; k++) {
						scatterH.append(linearHPoints[k][0], linearHPoints[k][1])
					}

					// Update V polarization constellation points
					scatterV.clear()
					for (var l = 0; l < linearVPoints.length; l++) {
						scatterV.append(linearVPoints[l][0], linearVPoints[l][1])
					}
				}
			}
		}
	}
}
