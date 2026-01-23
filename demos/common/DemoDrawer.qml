import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "." as Common

Drawer {
	id: demodrawer

	property int headerHeight: 0
	property string title: "Demo Settings"
	default property alias content: contentLayout.data
	property alias configManager: demoConfigManager
	property var endpoint: null

	implicitHeight: parent ? parent.height - headerHeight : 0
	y: headerHeight
	implicitWidth: 350
	edge: Qt.RightEdge
	dragMargin: 50
	modal: false

	background: Rectangle {
		radius: 0
		color: "#222a2f"
	}

	ScrollView {
		anchors.fill: parent
		clip: false
		ScrollBar.vertical.visible: true
		anchors.leftMargin: 20
		anchors.rightMargin: 0
		anchors.topMargin: 0
		anchors.bottomMargin: 0

		GridLayout {
			id: contentLayout
			Layout.alignment: Qt.AlignTop
			Layout.margins: 12
			columns: 2
			columnSpacing: 16
			rowSpacing: 10
			anchors.topMargin: 20 
			anchors.bottomMargin: 20
			anchors.rightMargin: 20

			Label {
				Layout.columnSpan: 2
				text: demodrawer.title
				font.pixelSize: 18
				color: "#ffffff"
				horizontalAlignment: Text.AlignHCenter
				topPadding: 20
			}
		}
	}

	Common.ConfigManager {
		id: demoConfigManager
		endpoint: demodrawer.endpoint
	}

	Component.onCompleted: {
		demoConfigManager.fetchAndApply()
	}
}
