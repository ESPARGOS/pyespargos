import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "." as Common

ApplicationWindow {
	id: root
	visible: true
	minimumWidth: 1280
	minimumHeight: 800

	Material.theme: Material.Dark
	Material.accent: "#227b3d"
	Material.roundedScale: Material.notRounded
	Material.primary: "#227b3d"

	color: "#11191e"
	title: "ESPARGOS Demo"

	header: ToolBar {
		id: header
		RowLayout {
			anchors.fill: parent

			ToolButton {
				text: "⚙ RX"
				font.pixelSize: Math.max(20, root.width / 80)
				MouseArea {
					anchors.fill: parent
					cursorShape: Qt.PointingHandCursor
					onClicked: poolDrawer.open()
				}
			}

			Item { Layout.fillWidth: true }

			Label {
				text: root.title
				font.pixelSize: Math.max(20, root.width / 80)
				color: "#ffffff"
				Layout.alignment: Qt.AlignVCenter | Qt.AlignHCenter
				horizontalAlignment: Text.AlignHCenter
			}

			Item { Layout.fillWidth: true }

			/*ToolButton {
				text: "Demo ⚙"
				font.pixelSize: Math.max(20, root.width / 80)
				MouseArea {
					anchors.fill: parent
					cursorShape: Qt.PointingHandCursor
					onClicked: demoDrawer.open()
				}
			}*/
		}
	}

	Common.PoolConfigDrawer {
		id: poolDrawer
		headerHeight: header.height
	}

	// Logo
	Image {
		source: "../common/img/espargos-logo.png"
		anchors.left: parent.left
		anchors.bottom: parent.bottom
		anchors.margins: 10
		width: 220
		fillMode: Image.PreserveAspectFit
		antialiasing: true
		z: 10
	}
}
