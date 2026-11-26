import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick.Shapes

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
	property bool tooltipsEnabled: true
	property string receiverDrawerLabel: "⚙ RX"

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
				text: root.receiverDrawerLabel
				font.pixelSize: Math.max(20, root.width / 80)
				visible: receiverDrawerComponent !== null
				MouseArea {
					anchors.fill: parent
					cursorShape: Qt.PointingHandCursor
					onClicked: {
						var drawer = receiverDrawerLoader.item
						if (drawer === null)
							return
						if (drawer.visible) {
							drawer.close()
						} else {
							drawer.open()
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

	/** Optional receiver configuration drawer, explicitly selected by the application. **/
	Loader {
		id: receiverDrawerLoader
		active: root.receiverDrawerComponent !== null
		sourceComponent: root.receiverDrawerComponent
		onLoaded: {
			if (item !== null)
				item.headerHeight = Qt.binding(() => header.height)
		}
	}

	/** Demo-specific configuration drawer **/
	property Component appDrawerComponent: null
	property var appDrawer: null

	onAppDrawerComponentChanged: {
		// defer creation to avoid header height being 0 at this point
		Qt.callLater( () => {
			appDrawer = appDrawerComponent.createObject(root.contentItem)
			appDrawer.headerHeight = Qt.binding(() => header.height)
		})
	}

	/** Optional application-provided receiver configuration drawer. **/
	property Component receiverDrawerComponent: null
	readonly property int rfSwitchMode: receiverDrawerLoader.item
		&& receiverDrawerLoader.item.rfSwitchMode !== undefined
		? receiverDrawerLoader.item.rfSwitchMode
		: -1

	Rectangle {
		id: rfSwitchWarning

		anchors.top: parent.top
		anchors.left: parent.left
		anchors.leftMargin: 20
		anchors.topMargin: 20
		width: rfSwitchWarningText.implicitWidth + 20
		height: 30
		color: "#b91c1c"
		opacity: 0.9
		radius: 6
		z: 10
		visible: root.rfSwitchMode === 0 || root.rfSwitchMode === 1

		Text {
			id: rfSwitchWarningText
			anchors.centerIn: parent
			text: root.rfSwitchMode === 0
				? "Warning: RF switch is isolated"
				: "Warning: RF switch uses reference input"
			color: "white"
			font.bold: true
			font.pixelSize: 14
		}
	}

	// Logo
	Image {
		source: "img/espargos-logo.png"
		anchors.left: parent.left
		anchors.bottom: parent.bottom
		anchors.margins: 10
		width: 220
		fillMode: Image.PreserveAspectFit
		antialiasing: true
		z: 10
	}

	property string calibrationMessage: backend.calibrationMessage
	Popup {
		objectName: "arrayProgressPopup"
		z: 100
		parent: Overlay.overlay
		focus: true
		anchors.centerIn: parent
		width: Math.min(440, parent.width - 48)
		padding: 28
		Material.theme: Material.Dark
		Overlay.modal: Rectangle { color: "#99000000" }
		background: Rectangle { color: "#222a2f"; border.color: "#405159"; radius: 6 }
		modal: true
		closePolicy: Popup.NoAutoClose
		visible: root.calibrationMessage.length > 0 || backend.startupPending
		contentItem: ColumnLayout {
			spacing: 16
			BusyIndicator {
				id: spinner
				implicitWidth: 60
				implicitHeight: 60
				padding: 6
				running: visible
				Layout.alignment: Qt.AlignHCenter
				contentItem: Shape {
					preferredRendererType: Shape.CurveRenderer
					rotation: (spinClock.elapsedTime * 180) % 360
					ShapePath {
						fillColor: "transparent"
						strokeColor: spinner.Material.accent
						strokeWidth: 4
						capStyle: ShapePath.RoundCap
						PathAngleArc {
							centerX: 24
							centerY: 24
							radiusX: 22
							radiusY: 22
							startAngle: -90
							sweepAngle: 260
						}
					}
					FrameAnimation {
						id: spinClock
						running: spinner.running
					}
				}
			}
			Label {
				text: root.calibrationMessage.length ? "Calibrating the array" : "Initializing the array"
				font.pixelSize: 22
				Layout.alignment: Qt.AlignHCenter
			}
			Label {
				text: root.calibrationMessage || "Preparing the receivers…"
				textFormat: Text.PlainText
				Layout.fillWidth: true
				horizontalAlignment: Text.AlignHCenter
				wrapMode: Text.Wrap
			}
		}
	}

	Connections {
		target: backend
		function onStartupErrorChanged() { root.showError("Initialization failed", backend.startupError) }
		Component.onCompleted: {
			if (backend.startupError) root.showError("Initialization failed", backend.startupError)
		}
	}

	Dialog {
		id: errorDialog
		z: 110
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
