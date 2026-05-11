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

	function showError(title, message) {
		errorDialog.title = title && title.length ? title : "Error"
		errorDialog.messageText = message && message.length ? message : ""
		errorDialog.open()
	}

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
		width: Math.min(parent.width * 0.8, 220)
		height: 220
		color: "#80000000"
		z: 100

		visible: backend.initializing

		BusyIndicator {
			anchors.centerIn: parent
			running: backend.initializing
			visible: backend.initializing
			width: 80
			height: 80
		}

		Label {
			anchors.horizontalCenter: parent.horizontalCenter
			anchors.top: parent.verticalCenter
			anchors.topMargin: 60
			text: "Initializing..."
			font.pixelSize: 18
			color: "#ffffff"
			visible: backend.initializing
		}
	}

	Dialog {
		id: errorDialog
		modal: true
		focus: true
		closePolicy: Popup.CloseOnEscape
		title: "Error"
		parent: Overlay.overlay
		property int maximumDialogHeight: Math.min(420, parent ? parent.height - 80 : 420)
		property int minimumDialogHeight: 180
		width: Math.min(760, parent ? parent.width - 60 : 760)
		height: Math.min(maximumDialogHeight, Math.max(minimumDialogHeight, errorTextArea.implicitHeight + 150))
		x: Math.round((parent.width - width) / 2)
		y: Math.round((parent.height - height) / 2)
		standardButtons: Dialog.Ok
		padding: 16

		property string messageText: ""

		Material.theme: Material.Dark
		Material.primary: "#227b3d"
		Material.accent: "#ffffff"
		Material.roundedScale: Material.notRounded

		contentItem: ScrollView {
			clip: true
			ScrollBar.vertical.policy: ScrollBar.AsNeeded

			TextArea {
				id: errorTextArea
				text: errorDialog.messageText
				readOnly: true
				wrapMode: TextArea.Wrap
				selectByMouse: true
				color: "#ffffff"
				selectionColor: "#227b3d"
				selectedTextColor: "#ffffff"
				textFormat: TextEdit.PlainText
				background: Rectangle {
					color: "transparent"
				}
			}
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
