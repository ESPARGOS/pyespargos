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

	property Component demoDrawerComponent: null
	property var demoDrawer: null

	function createDemoDrawer() {
		if (!demoDrawerComponent || demoDrawer) {
			return
		}
		if (demoDrawerComponent.status !== Component.Ready) {
			if (demoDrawerComponent.status === Component.Error) {
				console.error("Demo drawer component error:", demoDrawerComponent.errorString())
			}
			return
		}
		var drawerParent = root.contentItem
		if (!drawerParent) {
			return
		}
		demoDrawer = demoDrawerComponent.createObject(root)
		if (!demoDrawer) {
			return
		}
		if (demoDrawer.parent !== drawerParent) {
			demoDrawer.parent = drawerParent
		}
	}

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

			ToolButton {
				text: "Demo ⚙"
				font.pixelSize: Math.max(20, root.width / 80)
				visible: demoDrawerComponent !== null
				MouseArea {
					anchors.fill: parent
					cursorShape: Qt.PointingHandCursor
					onClicked: {
						if (!demoDrawer) {
							root.createDemoDrawer()
						}
						if (demoDrawer) {
							demoDrawer.open()
						}
					}
				}
			}
		}
	}

	Common.PoolConfigDrawer {
		id: poolDrawer
		headerHeight: header.height
	}

	onDemoDrawerComponentChanged: {
		if (demoDrawer) {
			demoDrawer.destroy()
			demoDrawer = null
		}
		if (!demoDrawerComponent) {
			return
		}
		Qt.callLater(createDemoDrawer)
	}

	Connections {
		target: header
		function onHeightChanged() {
			// No-op for demo drawer; contentItem size updates handle header height.
		}
	}

	Connections {
		target: demoDrawerComponent
		function onStatusChanged() {
			if (!demoDrawerComponent) return
			if (demoDrawerComponent.status === Component.Ready) {
				root.createDemoDrawer()
			} else if (demoDrawerComponent.status === Component.Error) {
				console.error("Demo drawer component error:", demoDrawerComponent.errorString())
			}
		}
	}

	Component.onCompleted: Qt.callLater(createDemoDrawer)

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
