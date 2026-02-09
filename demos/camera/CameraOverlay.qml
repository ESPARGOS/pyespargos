import QtQuick
import QtMultimedia
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
	color: "black"

	CaptureSession {
		id: captureSession
		camera: WebCam
		videoOutput: videoOutput
	}

	VideoOutput {
		id: videoOutput
        anchors.fill: parent
	}

	ShaderEffect {
		id: spatialSpectrumShader
		width: videoOutput.contentRect.width
		height: videoOutput.contentRect.height
		anchors.verticalCenter: videoOutput.verticalCenter
		anchors.horizontalCenter: videoOutput.horizontalCenter

		// This is the source for the beamspace canvas (power + delay colorization).
		property Canvas spatialSpectrumCanvas: Canvas {
			id: spatialSpectrumCanvas
			width: backend.resolutionAzimuth
			height: backend.resolutionElevation

			property var imageData: undefined
			function createImageData() {
				const ctx = spatialSpectrumCanvas.getContext("2d");
				imageData = ctx.createImageData(width, height);
			}

			onAvailableChanged: if(available) createImageData();
			onWidthChanged: if(available) createImageData();
			onHeightChanged: if(available) createImageData();

			onPaint: {
				if(imageData) {
					const ctx = spatialSpectrumCanvas.getContext("2d");
					ctx.drawImage(imageData, 0, 0);
				}
			}
		}

		// This is the source for the polarization canvas (polarization information)
		property Canvas polarizationCanvas: Canvas {
			id: polarizationCanvas
			width: backend.resolutionAzimuth
			height: backend.resolutionElevation

			property var polarizationImageData: undefined
			function createPolarizationImageData() {
				const ctx = polarizationCanvas.getContext("2d");
				polarizationImageData = ctx.createImageData(width, height);
			}

			onAvailableChanged: if(available) createPolarizationImageData();
			onWidthChanged: if(available) createPolarizationImageData();
			onHeightChanged: if(available) createPolarizationImageData();

			onPaint: {
				if(polarizationImageData) {
					const ctx = polarizationCanvas.getContext("2d");
					ctx.drawImage(polarizationImageData, 0, 0);
				}
			}
		}

		property variant spatialSpectrumCanvasSource: ShaderEffectSource {
			sourceItem: spatialSpectrumCanvas;
			hideSource: true
			smooth: true
		}

		property variant polarizationCanvasSource: ShaderEffectSource {
			sourceItem: polarizationCanvas;
			hideSource: true
			smooth: true
		}

		mesh: GridMesh {
			resolution: Qt.size(180, 90)
		}

		vertexShader: "spatialspectrum_vert.qsb"

		property bool rawBeamspace: backend.visualizationSpace === "beamspace"
		property bool flip: backend.cameraFlip
		property vector2d fov: Qt.vector2d(backend.fovAzimuth, backend.fovElevation)
		property real time: 0
		NumberAnimation on time {
			from: 0
			to: 6.283185307
			duration: 500
			loops: Animation.Infinite
			running: true
		}
		property bool polarizationVisible: backend.polarizationVisible
		property real gridSpacing: backend.gridSpacing

		fragmentShader: "spatialspectrum.qsb"

		// This is the source for the webcam image
		property variant cameraImage: ShaderEffectSource {
			sourceItem: videoOutput;
			sourceRect: videoOutput.contentRect;
			hideSource: true
			smooth: false
		}
	}

	Image {
		source: "img/beamspace_transform.png"
    	anchors.fill: spatialSpectrumShader
	    fillMode: Image.Stretch
		visible: backend.visualizationSpace === "beamspace"
    }

	/* Statistics display in bottom right corner */
	Rectangle {
		id: statsRectangle

		anchors.bottom: parent.bottom
		anchors.right: parent.right
		anchors.rightMargin: 10
		anchors.bottomMargin: 10
		width: 180
		height: 80
		color: "black"
		opacity: 0.8
		radius: 10

		Text {
			id: statsText
			text: "<b>Statistics</b><br/>RSSI: " + (isFinite(backend.rssi) ? backend.rssi.toFixed(2) + " dB" : "No Data") + "<br/>Antennas: " + backend.activeAntennas.toFixed(1)
			color: "white"
			font.family: "Monospace"
			font.pixelSize: 16
			anchors.top: parent.top
			anchors.left: parent.left
			anchors.topMargin: 10
			anchors.leftMargin: 10
		}
	}

	/* List of transmitter MACs in top right corner if enabled */
	Rectangle {
		id: macsListRectangle

		anchors.top: parent.top
		anchors.right: parent.right
		anchors.rightMargin: 10
		anchors.topMargin: 10
		width: 220
		height: 200
		color: "black"
		opacity: 0.8
		radius: 10
		visible: backend.macListEnabled

		ListModel {
			id: transmitterListModel
		}

		Item {
    		width: parent.width
    		height: parent.height
			Layout.fillWidth: true
			Layout.fillHeight: true
			anchors.fill: parent

			Component {
				id: transmitterListDelegate
				Item {
					width: 200
					height: 30
					anchors.margins: 5
					RowLayout {
						CheckBox {
							checked: visibilityChecked
							indicator.width: 18
							indicator.height: 18

							onCheckedChanged: function() {
								if (checked) {
									backend.setMacFilter(mac)
								} else {
									backend.clearMacFilter()
								}
								visibilityChecked = checked
								updateTransmitterList()
							}
						}
						Column {
							Text {
								text: "<b>MAC:</b> " + mac
								color: "#ffffff"
							}
						}
					}
				}
			}

			ListView {
				id: transmitterList
				anchors.fill: parent
				model: transmitterListModel
				delegate: transmitterListDelegate
			}
		}
	}

	function updateTransmitterList() {
		// Delete transmitters that should no longer be there
		let marked_for_removal = []
		let existing_macs = []
		let macFilterEnabled = false
		for (let i = 0; i < transmitterListModel.count; ++i) {
			let mac = transmitterListModel.get(i).mac
			if (transmitterListModel.get(i).visibilityChecked) {
				macFilterEnabled = true
			}
			if (!backend.macList.includes(mac)) {
				marked_for_removal.push(i)
			} else {
				existing_macs.push(mac)
			}
		}

		// If at least one MAC filter is enabled, mark all other MACs for removal
		if (macFilterEnabled) {
			for (let i = 0; i < transmitterListModel.count; ++i) {
				let mac = transmitterListModel.get(i).mac
				if (!transmitterListModel.get(i).visibilityChecked)
					marked_for_removal.push(i)
			}
		}

		for (const i of marked_for_removal.reverse()) {
			// Do not remove the item if it is the currently selected MAC filter
			if (!transmitterListModel.get(i).visibilityChecked) {
				transmitterListModel.remove(i)
			}
		}

		// Add transmitters that have appeared unless MAC filter is enabled
		if (!macFilterEnabled) {
			for (let i = 0; i < backend.macList.length; ++i) {
				let mac = backend.macList[i]
				if (!existing_macs.includes(mac))
					transmitterListModel.append({"mac" : mac, "visibilityChecked" : false})
			}
		}
	}

	Timer {
		interval: 50
		running: true
		repeat: true
		onTriggered: {
			backend.updateSpatialSpectrum()
		}
	}

	Connections {
		target: backend
		function onBeamspacePowerImagedataChanged(beamspacePowerImagedata) {
			//spatialSpectrumCanvas.imageData.data.set(new Uint8ClampedArray(beamspacePowerImagedata));
			if (spatialSpectrumCanvas.imageData === undefined)
				spatialSpectrumCanvas.createImageData();
			let len = spatialSpectrumCanvas.imageData.data.length;
			for (let i = 0; i < len; i++) {
				spatialSpectrumCanvas.imageData.data[i] = beamspacePowerImagedata[i];//(beamspacePowerImagedata[i]).qclamp(0, 255);
			}

			spatialSpectrumCanvas.requestPaint();
		}

		function onPolarizationImagedataChanged(polarizationImagedata) {
			if (polarizationCanvas.polarizationImageData === undefined)
				polarizationCanvas.createPolarizationImageData();
			let len = polarizationCanvas.polarizationImageData.data.length;
			for (let i = 0; i < len; i++) {
				polarizationCanvas.polarizationImageData.data[i] = polarizationImagedata[i];//(polarizationImagedata[i]).qclamp(0, 255);
			}

			polarizationCanvas.requestPaint();
		}

		function onMacListChanged(macList) {
			updateTransmitterList()
		}
	}
}
