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

	// Full screen management
	visibility: ApplicationWindow.Windowed
	Shortcut {
		sequence: "F11"
		onActivated: {
			if (window.visibility == ApplicationWindow.Windowed) {
				window.visibility = ApplicationWindow.FullScreen
			} else {
				window.visibility = ApplicationWindow.Windowed
			}
		}
	}

	Shortcut {
		sequence: "Esc"
		onActivated: window.close()
	}

	Material.theme: Material.Dark
	Material.accent: "#227b3d"
	Material.roundedScale: Material.notRounded
	Material.primary: "#227b3d"

	color: "#11191e"
	title: "ESPARGOS Demo"

	/** Header toolbar with title and buttons to open config drawers **/
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
					onClicked: {
						if (poolDrawer.visible) {
							poolDrawer.close()
						} else {
							poolDrawer.open()
						}
					}
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

			ToolButton {
				text: "Demo ⚙"
				font.pixelSize: Math.max(20, root.width / 80)
				visible: demoDrawerComponent !== null
				MouseArea {
					anchors.fill: parent
					cursorShape: Qt.PointingHandCursor
					onClicked: {
						if (demoDrawer.visible) {
							demoDrawer.close()
						} else {
							demoDrawer.open()
						}
					}
				}
			}
		}
	}

	/** RX pool configuration drawer **/
	Common.PoolConfigDrawer {
		id: poolDrawer
		headerHeight: header.height
	}

	/** Demo-specific configuration drawer **/
	property Component demoDrawerComponent: null
	property var demoDrawer: null

	onDemoDrawerComponentChanged: {
		// defer creation to avoid header height being 0 at this point
		Qt.callLater( () => {
			demoDrawer = demoDrawerComponent.createObject(root.contentItem)
			demoDrawer.headerHeight = header.height
		})
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
