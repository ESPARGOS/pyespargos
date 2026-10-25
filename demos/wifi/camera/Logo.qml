import QtQuick

Item {
	implicitHeight: window.height / 18
	width: image.sourceSize.width * height / image.sourceSize.height
	antialiasing: true
	
	property alias source: image.source

	Image {
		id: image
		anchors.fill: parent
	}
}