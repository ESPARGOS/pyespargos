pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtCharts
import "../common" as Common

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 1280
	minimumHeight: 800

	title: "IQ Signal Analyzer"

	// Universal antenna colour coding (same palette/order as the web interface)
	property var colorCycle: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]
	property var antidToColorIndex: [4, 5, 6, 7, 0, 1, 2, 3]
	function antennaColor(antid) { return colorCycle[antidToColorIndex[antid]] }
	function formatDbfs(value) { return isFinite(value) ? value.toFixed(1) + " dBFS" : "−∞ dBFS" }

	// Front view of the array: top row antids 4..7, bottom row 0..3
	property var frontViewRows: [[4, 5, 6, 7], [0, 1, 2, 3]]

	/** RX drawer: the reusable IQ capture settings (replaces the CSI pool drawer) **/
	receiverDrawerComponent: Component {
		Common.IQRxDrawer {
			controller: iqcontrol
		}
	}

	/** App drawer: app-specific display settings only **/
	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Display"
			endpoint: appconfig
			// Keep this addon's grid narrower than the drawer viewport. AppDrawer
			// then has no horizontal overflow to scroll, without changing the
			// shared component used by WiFi demos.
			contentLayout.width: Math.max(0, appDrawer.width - 40)

			Label { text: "Mode"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				property string configKey: "display_mode"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180
				textRole: "text"
				valueRole: "value"
				model: [
					{ value: "power", text: "Power waterfall" },
					{ value: "phase", text: "Relative phase" },
					{ value: "time", text: "Time domain I/Q" },
					{ value: "constellation", text: "I/Q constellation" },
					{ value: "spectrum", text: "Amplitude spectrum" }
				]
				currentValue: "power"
			}

			Label { text: "FFT"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				property string configKey: "fft_size"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180
				model: [256, 512, 1024]
				currentIndex: 2
				ToolTip.visible: hovered
				ToolTip.text: "Samples per display update in every mode: one waterfall row, chart trace or constellation spans FFT/256 consecutive chunks, so the trigger must deliver contiguous runs of at least that many chunks (e.g. interval burst >= 4 for 1024)."
			}

			Label { text: "Use Calibration"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				property string configKey: "apply_calibration"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
				ToolTip.visible: hovered
				ToolTip.text: "Apply the fine per-sensor time/phase calibration to the relative-phase display. No effect until a calibration has been run (drawer button or startup)."
			}

			Label { text: "Complete only"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				property string configKey: "complete_only"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: false
				ToolTip.visible: hovered
				ToolTip.text: "On: render only sets where ALL sensors delivered the chunk (incomplete data is skipped). Off: render everything; sensors missing a chunk show black / placeholder rows."
			}
		}
	}

	/** 2x4 antenna displays, front view; one tab per board on multi-board
	    coherent arrays (only the active board is rendered) **/
	ColumnLayout {
		anchors.fill: parent
		anchors.margins: 10
		spacing: 8

		TabBar {
			id: boardTabs
			visible: backend.boardCount > 1
			Layout.fillWidth: true
			onCurrentIndexChanged: backend.set_active_board(currentIndex)
			Repeater {
				model: backend.boardCount
				TabButton {
					required property int index
					text: "Array " + (index + 1) + "  ·  " + backend.boardHosts[index]
				}
			}
		}

		Repeater {
			model: window.frontViewRows.length
			RowLayout {
				required property int index
				property var rowAntids: window.frontViewRows[index]
				Layout.fillWidth: true
				Layout.fillHeight: true
				spacing: 8

				Repeater {
					model: rowAntids.length
					Rectangle {
						id: antennaPanel
						required property int index
						property int antid: rowAntids[index]
						Layout.fillWidth: true
						Layout.fillHeight: true
						color: "#101014"
						border.color: window.antennaColor(antid)
						border.width: 2
						radius: 4

						ColumnLayout {
							anchors.fill: parent
							anchors.margins: 6
							spacing: 4

							Label {
								text: "Antenna " + antennaPanel.antid
								color: window.antennaColor(antennaPanel.antid)
								font.bold: true
								font.pixelSize: 13
							}

							Item {
								Layout.fillWidth: true
								Layout.fillHeight: true

								Image {
									id: displayImage
									anchors.fill: parent
									visible: !backend.timeDomain && !backend.constellation && !backend.spectrum
									fillMode: Image.Stretch
									cache: false
									smooth: false
									source: "image://display" + antennaPanel.antid

									property int frameRevision: 0
									Connections {
										target: backend
										function onWaterfallFrameReady() {
											displayImage.frameRevision += 1
											displayImage.source = "image://display" + antennaPanel.antid
												+ "?" + displayImage.frameRevision
										}
									}
								}

								Loader {
									anchors.fill: parent
									active: backend.timeDomain
									sourceComponent: Component {
										ChartView {
											id: timeChart
											property int antid: antennaPanel.antid
											legend.visible: false
											antialiasing: true
											backgroundColor: "#101014"
											backgroundRoundness: 0
											dropShadowEnabled: false
											margins { top: 0; bottom: 0; left: 0; right: 0 }

											ValueAxis {
												id: sampleAxis
												min: 0
												max: Math.max(1, backend.traceSampleCount - 1)
												labelsVisible: false
												gridVisible: false
												lineVisible: false
											}

											ValueAxis {
												id: adcAxis
												min: -backend.adcFullScale
												max: backend.adcFullScale
												tickCount: 5
												labelsVisible: false
												lineVisible: false
												gridLineColor: "#37373b"
											}

											LineSeries {
												id: iTrace
												axisX: sampleAxis
												axisY: adcAxis
												color: "#2c7fb8"
												width: 1.25
												useOpenGL: Qt.platform.os === "linux"
											}

											LineSeries {
												id: qTrace
												axisX: sampleAxis
												axisY: adcAxis
												color: "#d95f0e"
												width: 1.25
												useOpenGL: Qt.platform.os === "linux"
											}

											Timer {
												interval: 1000 / 25
												running: true
												repeat: true
												onTriggered: {
													const stats = backend.updateTimeChart(timeChart.antid, iTrace, qTrace)
													if (stats.rmsDbfs !== undefined) {
														timeStats.rmsDbfs = stats.rmsDbfs
														timeStats.peakDbfs = stats.peakDbfs
													}
												}
											}
										}
									}
								}

								Loader {
									anchors.fill: parent
									active: backend.spectrum
									sourceComponent: Component {
										ChartView {
											id: spectrumChart
											property int antid: antennaPanel.antid
											legend.visible: false
											antialiasing: true
											backgroundColor: "#101014"
											backgroundRoundness: 0
											dropShadowEnabled: false
											margins { top: 0; bottom: 0; left: 0; right: 0 }

											ValueAxis {
												id: spectrumFrequencyAxis
												min: 2397
												max: 2477
												tickCount: 5
												labelFormat: "%.1f"
												labelsColor: "#9aa4b2"
												labelsFont.pixelSize: 8
												lineVisible: false
												gridLineColor: "#37373b"
												titleText: "<font color='#9aa4b2'>Frequency [MHz]</font>"
												titleFont.pixelSize: 9
											}

											ValueAxis {
												id: spectrumAmplitudeAxis
												min: -90
												max: 0
												tickCount: 4
												labelFormat: "%.0f"
												labelsColor: "#9aa4b2"
												labelsFont.pixelSize: 8
												lineVisible: false
												gridLineColor: "#37373b"
												titleText: "<font color='#9aa4b2'>Amplitude [dBFS]</font>"
												titleFont.pixelSize: 9
											}

											LineSeries {
												id: spectrumLine
												axisX: spectrumFrequencyAxis
												axisY: spectrumAmplitudeAxis
												color: window.antennaColor(spectrumChart.antid)
												width: 1.25
												useOpenGL: Qt.platform.os === "linux"
											}

											Timer {
												interval: 1000 / 25
												running: true
												repeat: true
												onTriggered: backend.updateSpectrumChart(spectrumChart.antid, spectrumLine, spectrumFrequencyAxis)
											}
										}
									}
								}

								Loader {
									anchors.centerIn: parent
									width: Math.min(parent.width, parent.height)
									height: width
									active: backend.constellation
									sourceComponent: Component {
										ChartView {
											id: constellationChart
											property int antid: antennaPanel.antid
											legend.visible: false
											antialiasing: true
											backgroundColor: "#101014"
											backgroundRoundness: 0
											dropShadowEnabled: false
											margins { top: 0; bottom: 0; left: 0; right: 0 }

											ValueAxis {
												id: constellationIAxis
												min: -backend.adcFullScale
												max: backend.adcFullScale
												tickCount: 5
												labelsVisible: false
												lineVisible: false
												gridLineColor: "#37373b"
											}

											ValueAxis {
												id: constellationQAxis
												min: -backend.adcFullScale
												max: backend.adcFullScale
												tickCount: 5
												labelsVisible: false
												lineVisible: false
												gridLineColor: "#37373b"
											}

											ScatterSeries {
												id: constellationPoints
												axisX: constellationIAxis
												axisY: constellationQAxis
												color: window.antennaColor(constellationChart.antid)
												borderColor: "transparent"
												markerSize: 3
												useOpenGL: Qt.platform.os === "linux"
											}

											Timer {
												interval: 1000 / 25
												running: true
												repeat: true
												onTriggered: backend.updateConstellationChart(constellationChart.antid, constellationPoints)
											}

											Label {
												anchors.top: parent.top
												anchors.left: parent.left
												anchors.margins: 4
												text: "Q"
												color: "#9aa4b2"
												font.pixelSize: 10
												z: 2
											}

											Label {
												anchors.right: parent.right
												anchors.bottom: parent.bottom
												anchors.margins: 4
												text: "I"
												color: "#9aa4b2"
												font.pixelSize: 10
												z: 2
											}
										}
									}
								}

								Label {
									id: timeStats
									property real rmsDbfs: NaN
									property real peakDbfs: NaN
									anchors.top: parent.top
									anchors.right: parent.right
									anchors.margins: 6
									visible: backend.timeDomain
									z: 2
									text: "RMS   " + window.formatDbfs(rmsDbfs) + "\nPeak  " + window.formatDbfs(peakDbfs)
									color: "#d8dee9"
									font.family: "monospace"
									font.pixelSize: 11
									padding: 5
									background: Rectangle {
										color: "#c0101014"
										border.color: "#50545a"
										border.width: 1
										radius: 3
									}
								}
							}
						}
					}
				}
			}
		}
	}

	/** Adapt the waterfall image width to the window; debounce resize events.
	 *  Qt Charts modes render directly at their item's resolution. **/
	onWidthChanged: displayWidthDebounce.restart()
	Component.onCompleted: displayWidthDebounce.restart()
	Timer {
		id: displayWidthDebounce
		interval: 300
		repeat: false
		onTriggered: backend.set_display_width(Math.round((window.width - 60) / 4))
	}
}
