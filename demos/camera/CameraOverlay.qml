import QtQuick
import QtMultimedia
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

		// This is the source for the beamspace canvas.
		// It is unused if MUSIC mode is enabled.
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

			onPaint: {
				if(imageData) {
					const ctx = spatialSpectrumCanvas.getContext("2d");
					ctx.drawImage(imageData, 0, 0);
				}
			}
		}

		property variant spatialSpectrumCanvasSource: ShaderEffectSource {
			sourceItem: spatialSpectrumCanvas;
			hideSource: true
			smooth: true
		}

		mesh: GridMesh {
			resolution: Qt.size(180, 90)
		}

		vertexShader: "spatialspectrum_vert.qsb"

		// This is the hacky way to pass the spatial spectrum to the shader if MUSIC mode is enabled.
		property matrix4x4 horizontalSpatialSpectrum0
		property matrix4x4 horizontalSpatialSpectrum1
		property matrix4x4 horizontalSpatialSpectrum2
		property matrix4x4 horizontalSpatialSpectrum3
		property matrix4x4 horizontalSpatialSpectrum4
		property matrix4x4 horizontalSpatialSpectrum5
		property matrix4x4 horizontalSpatialSpectrum6
		property matrix4x4 horizontalSpatialSpectrum7

		property matrix4x4 verticalSpatialSpectrum0
		property matrix4x4 verticalSpatialSpectrum1
		property matrix4x4 verticalSpatialSpectrum2
		property matrix4x4 verticalSpatialSpectrum3
		property matrix4x4 verticalSpatialSpectrum4
		property matrix4x4 verticalSpatialSpectrum5
		property matrix4x4 verticalSpatialSpectrum6
		property matrix4x4 verticalSpatialSpectrum7

		property bool musicMode: backend.music
		property bool fftMode: backend.isFFTBeamspace
		property bool rawBeamspace: backend.rawBeamspace
		property vector2d fov: Qt.vector2d(backend.fovAzimuth, backend.fovElevation)

		fragmentShader: "spatialspectrum.qsb"

		// This is the source for the webcam image
		property variant cameraImage: ShaderEffectSource {
			sourceItem: videoOutput;
			sourceRect: videoOutput.contentRect;
			hideSource: true
			smooth: false
		}
	}

	Logo {
		source: "img/espargos_logo.svg"
		anchors.bottom: spatialSpectrumShader.bottom
	}

	Image {
		source: "img/beamspace_transform.png"
    	anchors.fill: parent
	    fillMode: Image.Stretch
		visible: backend.rawBeamspace
    }

	Rectangle {
		id: statsRectangle

		anchors.bottom: parent.bottom
		anchors.right: parent.right
		anchors.rightMargin: 10
		anchors.bottomMargin: 10
		width: 270
		height: 58
		color: "black"
		opacity: 0.8
		radius: 4

		Text {
			id: statsText
			text: "<b>RSSI</b>: " + (isFinite(backend.rssi) ? backend.rssi.toFixed(2) + " dB" : "No Data") //+ "<br/>Antennas: " + backend.activeAntennas.toFixed(1)
			color: "white"
			font.family: "Monospace"
			font.pixelSize: 25
			anchors.top: parent.top
			anchors.left: parent.left
			anchors.topMargin: 10
			anchors.leftMargin: 20
		}
	}

	Timer {
		interval: 50
		running: true
		repeat: true
		onTriggered: {
			backend.updateSpatialSpectrum()

			const verticalSpectrum = backend.verticalSpectrum
			const horizontalSpectrum = backend.horizontalSpectrum

			if (backend.music) {
				spatialSpectrumShader.verticalSpatialSpectrum0 = Qt.matrix4x4(verticalSpectrum.slice(  0,  16))
				spatialSpectrumShader.verticalSpatialSpectrum1 = Qt.matrix4x4(verticalSpectrum.slice( 16,  32))
				spatialSpectrumShader.verticalSpatialSpectrum2 = Qt.matrix4x4(verticalSpectrum.slice( 32,  48))
				spatialSpectrumShader.verticalSpatialSpectrum3 = Qt.matrix4x4(verticalSpectrum.slice( 48,  64))
				spatialSpectrumShader.verticalSpatialSpectrum4 = Qt.matrix4x4(verticalSpectrum.slice( 64,  80))
				spatialSpectrumShader.verticalSpatialSpectrum5 = Qt.matrix4x4(verticalSpectrum.slice( 80,  96))
				spatialSpectrumShader.verticalSpatialSpectrum6 = Qt.matrix4x4(verticalSpectrum.slice( 96, 112))
				spatialSpectrumShader.verticalSpatialSpectrum7 = Qt.matrix4x4(verticalSpectrum.slice(112, 128))

				spatialSpectrumShader.horizontalSpatialSpectrum0 = Qt.matrix4x4(horizontalSpectrum.slice(  0,  16))
				spatialSpectrumShader.horizontalSpatialSpectrum1 = Qt.matrix4x4(horizontalSpectrum.slice( 16,  32))
				spatialSpectrumShader.horizontalSpatialSpectrum2 = Qt.matrix4x4(horizontalSpectrum.slice( 32,  48))
				spatialSpectrumShader.horizontalSpatialSpectrum3 = Qt.matrix4x4(horizontalSpectrum.slice( 48,  64))
				spatialSpectrumShader.horizontalSpatialSpectrum4 = Qt.matrix4x4(horizontalSpectrum.slice( 64,  80))
				spatialSpectrumShader.horizontalSpatialSpectrum5 = Qt.matrix4x4(horizontalSpectrum.slice( 80,  96))
				spatialSpectrumShader.horizontalSpatialSpectrum6 = Qt.matrix4x4(horizontalSpectrum.slice( 96, 112))
				spatialSpectrumShader.horizontalSpatialSpectrum7 = Qt.matrix4x4(horizontalSpectrum.slice(112, 128))
			}
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
	}
}
