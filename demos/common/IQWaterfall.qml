import QtQuick
import QtQuick.Controls

Rectangle {
	id: root
	property var model
	property string providerName: "iq-camera-waterfall"

	color: "#dc07090b"
	border.color: "#708090"
	border.width: 1
	radius: 5
	clip: true

	Text {
		id: title
		anchors.left: parent.left
		anchors.right: parent.right
		anchors.top: parent.top
		anchors.margins: 7
		text: root.model ? root.model.spectrumStatus : "IQ spectrum"
		color: "white"
		font.pixelSize: 12
		font.bold: true
		elide: Text.ElideRight
	}

	Image {
		anchors.left: parent.left
		anchors.right: parent.right
		anchors.top: title.bottom
		anchors.bottom: frequencyLabels.top
		anchors.margins: 7
		cache: false
		fillMode: Image.Stretch
		smooth: false
		source: root.model ? "image://" + root.providerName + "/frame?" + root.model.waterfallGeneration : ""
	}

	Text {
		id: frequencyLabels
		anchors.left: parent.left
		anchors.right: parent.right
		anchors.bottom: parent.bottom
		anchors.leftMargin: 7
		anchors.rightMargin: 7
		anchors.bottomMargin: 4
		text: root.model ? root.model.frequencyLowMhz.toFixed(1) + " MHz                                                        " + root.model.frequencyHighMhz.toFixed(1) + " MHz" : ""
		color: "#d8e2eb"
		font.pixelSize: 10
		horizontalAlignment: Text.AlignHCenter
		elide: Text.ElideMiddle
	}
}
