import QtQuick
import QtMultimedia
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    property int updateInterval: 50
	color: "black"

	CaptureSession {
		id: captureSession
		camera: CameraView.enabled ? WebCam : null
		videoOutput: videoOutput
	}

	VideoOutput {
		id: videoOutput
        objectName: "cameraVideo"
        anchors.fill: parent
	}

	ShaderEffect {
		id: spatialSpectrumShader
		width: CameraView.enabled && videoOutput.contentRect.width > 0 ? videoOutput.contentRect.width : parent.width
		height: CameraView.enabled && videoOutput.contentRect.height > 0 ? videoOutput.contentRect.height : parent.height
		anchors.verticalCenter: videoOutput.verticalCenter
		anchors.horizontalCenter: videoOutput.horizontalCenter

        property variant spatialSpectrumCanvasSource: Image {
            source: overlayModel.spatialImageSource
            visible: false
            cache: false
            smooth: true
        }
        property variant polarizationCanvasSource: Image {
            source: overlayModel.polarizationImageSource
            visible: false
            cache: false
            smooth: true
        }

		mesh: GridMesh {
			resolution: Qt.size(180, 90)
		}

		vertexShader: "spatialspectrum_vert.qsb"

		property bool rawBeamspace: overlayModel.visualizationSpace === "beamspace"
		property bool flipCamera: CameraView.flipCamera
		property bool flipOverlay: CameraView.flipOverlay
		property vector2d fov: Qt.vector2d(CameraView.fovAzimuth, CameraView.fovElevation)
		property real time: 0
		Timer {
			interval: 16
			repeat: true
			property real lastTick: 0
			running: spatialSpectrumShader.polarizationVisible && overlayModel.polarizationSpeed > 0
			onRunningChanged: lastTick = Date.now()
			// Explicit pacing also bounds GPU work when swap waiting is disabled.
			onTriggered: {
				const now = Date.now()
				const elapsed = Math.min(0.1, (now - lastTick) / 1000)
				lastTick = now
				spatialSpectrumShader.time = (spatialSpectrumShader.time
					+ Math.PI * overlayModel.polarizationSpeed * elapsed) % (2 * Math.PI)
			}
		}
		property bool polarizationVisible: overlayModel.polarizationVisible
        property int renderStyle: overlayModel.renderStyle
		property vector2d polarizationTexelSize: Qt.vector2d(1.0 / overlayModel.resolutionAzimuth, 1.0 / overlayModel.resolutionElevation)
		property real gridSpacing: overlayModel.gridSpacing
		property real azimuthCorrection: overlayModel.azimuth_correction
		property real elevationCorrection: overlayModel.elevation_correction

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
        objectName: "beamspaceAxes"
        x: spatialSpectrumShader.x
        y: spatialSpectrumShader.y
        width: spatialSpectrumShader.width
        height: spatialSpectrumShader.height
        visible: overlayModel.visualizationSpace === "beamspace"
        source: "img/beamspace_transform.svg"
        sourceSize: Qt.size(width, height)
        fillMode: Image.Stretch
    }

    Repeater {
        model: overlayModel.markers
        Item {
            id: directionMarker
            required property var modelData
            x: spatialSpectrumShader.x + modelData.x * spatialSpectrumShader.width
            y: spatialSpectrumShader.y + modelData.y * spatialSpectrumShader.height
            Rectangle { visible: !parent.modelData.offscreen; anchors.centerIn: parent; width: 16; height: 16; radius: 8; color: "transparent"; border.color: "#e9fbff" }
            Text { visible: parent.modelData.offscreen; anchors.centerIn: parent; text: "➤"; rotation: parent.modelData.rotation; color: "#ffd970"; font.pixelSize: 22 }
            Rectangle {
                x: directionMarker.modelData.x > 0.7 ? -width - 12 : 12
                y: -height - 10
                width: directionLabel.implicitWidth + 8
                height: directionLabel.implicitHeight + 6
                color: "#c0182229"
                radius: 3
                Text {
                    id: directionLabel
                    anchors.centerIn: parent
                    text: directionMarker.modelData.label
                    color: directionMarker.modelData.offscreen ? "#ffd970" : "white"
                    font.pixelSize: 13
                }
            }
        }
    }

    Timer { interval: parent.updateInterval; repeat: true; running: !backend.initializing; onTriggered: backend.updateSpatialSpectrum() }

}
