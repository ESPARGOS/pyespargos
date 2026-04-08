import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import QtQuick
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 900
	minimumHeight: 560
	property bool axisTextReady: false
	property int equationImageRevision: 0

	color: "#11191e"
	title: "Stochastic Fading Demo"

	Component.onCompleted: axisTextStartupTimer.start()

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Settings"
			endpoint: appconfig

			Label { Layout.columnSpan: 2; text: "Histogram Settings"; color: "#9fb3c8" }

			Label { text: "Bins"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: binSpinBox
				property string configKey: "bin_count"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 148
				from: 5
				to: 200
				value: 50
			}

			Label { text: "Max Samples"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: maxSamplesSpinBox
				property string configKey: "max_samples"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 194
				from: 100
				to: 300000
				stepSize: 100
				value: 300000
			}

			Label { text: "Fit Overlay"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: fitModelCombo
				property string configKey: "fit_model"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 148

				model: [
					{ value: "none", text: "Off" },
					{ value: "rayleigh", text: "Rayleigh" },
					{ value: "rice", text: "Rice" }
				]
				textRole: "text"
				valueRole: "value"
				currentValue: "rayleigh"
			}

			Label { text: "Comp. AGC"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: compensateSwitch
				property string configKey: "compensate_rssi"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
			}

			Item { Layout.columnSpan: 2; width: 1; height: 6; visible: true }

			Button {
				Layout.columnSpan: 2
				Layout.fillWidth: true
				text: "Reset Histogram"
				onClicked: backend.resetHistogram()
			}

			Common.GenericAppSettings {
				id: genericAppSettings
				insertBefore: genericAppSettingsAnchor
				controlWidth: 148
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

			Rectangle {
				Layout.columnSpan: 2
				width: 1
				height: 30
				color: "transparent"
			}
		}
	}

	ColumnLayout {
		anchors.fill: parent
		spacing: 4

		Rectangle {
			Layout.fillWidth: true
			Layout.preferredHeight: 80
			color: "#151f26"
			radius: 4

			RowLayout {
				anchors.fill: parent
				anchors.leftMargin: 18
				anchors.rightMargin: 18
				anchors.topMargin: 12
				anchors.bottomMargin: 12
				spacing: 18

				ColumnLayout {
					Layout.fillWidth: true
					Layout.fillHeight: true
					spacing: 0

					RowLayout {
						spacing: 12

						Rectangle {
							Layout.preferredWidth: 156
							Layout.preferredHeight: 40
							radius: 4
							color: "#1d2a33"
							border.color: "#2e4351"
							clip: true

							Rectangle {
								width: parent.width * backend.sampleFillFraction
								height: parent.height
								radius: 4
								color: "#23495c"
								opacity: backend.sampleBufferWrapped ? 0.18 : 0.38
							}

							Rectangle {
								visible: backend.sampleBufferWrapped
								width: Math.max(14, parent.width * 0.1)
								height: parent.height
								x: (parent.width + width) * backend.sampleOverwritePhase - width
								radius: 4
								color: "#4cc9f0"
								opacity: 0.22
							}

							RowLayout {
								anchors.fill: parent
								anchors.leftMargin: 12
								anchors.rightMargin: 12
								spacing: 10

								Label {
									text: "Samples"
									color: "#8fa6b8"
									font.pixelSize: 12
								}

								Item { Layout.fillWidth: true }

								Label {
									text: backend.sampleCount
									color: "#f2f4f8"
									font.pixelSize: 17
									font.bold: true
								}
							}
						}

						Rectangle {
							Layout.preferredWidth: backend.fitModel === "rice" ? 270 : 232
							Layout.preferredHeight: 40
							visible: backend.fitModel !== "none"
							radius: 4
							color: "#1d2a33"
							border.color: "#2e4351"

							RowLayout {
								anchors.fill: parent
								anchors.leftMargin: 12
								anchors.rightMargin: 12
								spacing: 10

								Label {
									text: "Parameters"
									color: "#8fa6b8"
									font.pixelSize: 12
								}

								Item { Layout.fillWidth: true }

								Label {
									text: backend.fitModel === "rice"
										? "ν = " + backend.fitNu.toFixed(4) + "   σ = " + backend.fitSigma.toFixed(4)
										: "σ = 1/√2"
									color: "#f2f4f8"
									font.pixelSize: 15
									font.bold: true
								}
							}
						}
					}
				}

				Rectangle {
					Layout.preferredWidth: {
						const availableHeight = Math.max(1, height - 20)
						const aspectRatio = equationImage.implicitHeight > 0 ? equationImage.implicitWidth / equationImage.implicitHeight : 1
						return availableHeight * aspectRatio + 16
					}
					Layout.fillHeight: true
					Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
					radius: 4
					color: "#11191e"
					border.color: "#24343f"
					visible: backend.fitModel !== "none"

					Item {
						anchors.fill: parent
						anchors.leftMargin: 8
						anchors.rightMargin: 8
						anchors.topMargin: 10
						anchors.bottomMargin: 10

						Image {
							id: equationImage
							property real aspectRatio: implicitHeight > 0 ? implicitWidth / implicitHeight : 1
							height: Math.min(parent.height, parent.width / aspectRatio)
							width: height * aspectRatio
							x: parent.width - width
							y: (parent.height - height) / 2
							source: (backend.fitModel === "rice" ? "rice-equation.png" : "rayleigh-equation.png") + "?rev=" + window.equationImageRevision
							fillMode: Image.PreserveAspectFit
							smooth: true
							mipmap: true
							asynchronous: false
						}
					}
				}

				Rectangle {
					Layout.preferredWidth: 180
					Layout.fillHeight: true
					Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
					radius: 4
					color: "#1d2a33"
					border.color: "#2e4351"
					visible: backend.fitModel === "none"

					Label {
						anchors.centerIn: parent
						text: "Histogram Only"
						color: "#f2f4f8"
						font.pixelSize: 14
						font.bold: true
					}
				}
			}
		}

		ChartView {
			id: histogramChart
			Layout.fillWidth: true
			Layout.fillHeight: true
			legend.visible: false
			legend.labelColor: "#e0e0e0"
			antialiasing: true
			backgroundColor: "#151f26"

			axes: [
				ValueAxis {
					id: magnitudeAxis
					min: 0
					max: 1
					titleText: ""
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickType: ValueAxis.TicksDynamic
					labelsColor: window.axisTextReady ? "#e0e0e0" : "transparent"
				},
				ValueAxis {
					id: densityAxis
					min: 0
					max: 1
					titleText: window.axisTextReady ? "<font color=\"#e0e0e0\">Probability Density</font>" : ""
					titleFont.bold: false
					gridLineColor: "#c0c0c0"
					tickType: ValueAxis.TicksDynamic
					labelsColor: window.axisTextReady ? "#e0e0e0" : "transparent"
				}
			]

			LineSeries { id: histogramLower; axisX: magnitudeAxis; axisY: densityAxis; visible: false }
			LineSeries { id: histogramUpper; axisX: magnitudeAxis; axisY: densityAxis; visible: false }

			AreaSeries {
				name: ""
				axisX: magnitudeAxis
				axisY: densityAxis
				upperSeries: histogramUpper
				lowerSeries: histogramLower
				color: "#4cc9f0"
				borderColor: "#4cc9f0"
				opacity: 0.45
			}

			LineSeries {
				id: fitSeries
				name: ""
				axisX: magnitudeAxis
				axisY: densityAxis
				color: "#ffb703"
				width: 3
				pointsVisible: false
				useOpenGL: false
			}
		}

		Item {
			Layout.fillWidth: true
			Layout.preferredHeight: 88

			Image {
				property real aspectRatio: implicitHeight > 0 ? implicitWidth / implicitHeight : 1
				anchors.centerIn: parent
				source: "xaxis-equation.png?rev=" + window.equationImageRevision
				height: Math.min(parent.height - 12, (parent.width - 24) / aspectRatio)
				width: height * aspectRatio
				visible: window.axisTextReady
				fillMode: Image.PreserveAspectFit
				smooth: true
				mipmap: true
				asynchronous: false
			}
		}
	}

	Timer {
		id: axisTextStartupTimer
		interval: 120
		repeat: false
		onTriggered: {
			window.axisTextReady = true
			window.equationImageRevision += 1
		}
	}

	Timer {
		interval: 100
		running: true
		repeat: true
		onTriggered: backend.updateDistribution(histogramUpper, histogramLower, fitSeries, magnitudeAxis, densityAxis)
	}
}
