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
	visibility: backend.kioskMode ? ApplicationWindow.FullScreen : ApplicationWindow.Windowed
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
		enabled: !backend.kioskMode
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
				text: "App ⚙"
				font.pixelSize: Math.max(20, root.width / 80)
				visible: appDrawerComponent !== null
				MouseArea {
					anchors.fill: parent
					cursorShape: Qt.PointingHandCursor
					onClicked: {
						if (appDrawer.visible) {
							appDrawer.close()
						} else {
							appDrawer.open()
						}
					}
				}
			}
		}
	}

	/** RX pool configuration drawer **/
	Common.PoolDrawer {
		id: poolDrawer
		headerHeight: header.height
	}

	/** Demo-specific configuration drawer **/
	property Component appDrawerComponent: null
	property var appDrawer: null

	onAppDrawerComponentChanged: {
		// defer creation to avoid header height being 0 at this point
		Qt.callLater( () => {
			appDrawer = appDrawerComponent.createObject(root.contentItem)
			appDrawer.headerHeight = header.height
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

	Rectangle {
		anchors.centerIn: parent
		width: Math.min(parent.width * 0.5, 420)
		height: 100
		radius: 12
		color: "#80000000"
		border.color: "#40ffffff"
		border.width: 2
		z: 100

		visible: backend.initializing

		Text {
			anchors.centerIn: parent
			text: "Initializing..."
			color: "#ffffff"
			font.pixelSize: 26
			font.bold: true
		}
	}

	// Kiosk mode: Exit button in bottom-right corner
	Button {
		id: kioskExitButton
		visible: backend.kioskMode
		z: 10
		anchors.right: parent.right
		anchors.bottom: parent.bottom
		anchors.margins: 10
		text: "✕ Exit"
		flat: true
		font.pixelSize: 14
		Material.background: "#227b3d"
		Material.foreground: "#ffffffff"
		onClicked: kioskExitDialog.open()
	}

	// Kiosk mode: Exit confirmation dialog
	Dialog {
		id: kioskExitDialog
		title: "Exit Application"
		anchors.centerIn: parent
		modal: true
		standardButtons: Dialog.Cancel
		z: 200

		Material.roundedScale: Material.SmallScale

		ColumnLayout {
			spacing: 16
			width: parent.width

			Label {
				text: "What would you like to do?"
				font.pixelSize: 14
				Layout.fillWidth: true
				wrapMode: Text.WordWrap
			}

			Button {
				text: "Quit Application"
				Layout.fillWidth: true
				Material.background: Material.primary
				Material.foreground: "#ffffff"
				onClicked: {
					kioskExitDialog.close()
					Qt.quit()
				}
			}

			Button {
				text: "Shut Down Computer"
				Layout.fillWidth: true
				Material.background: "#b71c1c"
				Material.foreground: "#ffffff"
				onClicked: {
					kioskExitDialog.close()
					backend.shutdownComputer()
				}
			}
		}
	}
}
