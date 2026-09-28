import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "." as Common

/**
 * RX-side drawer for IQ-sampling demos — the counterpart of PoolDrawer for
 * demos that work with raw synchronized IQ samples instead of CSI. Contains
 * the reusable IQ capture settings (tuning frequency, sample rate, analog
 * filter bandwidth, RF switch, gain, triggers, array sync and phase
 * calibration).
 *
 * Usage (in a Common.ESPARGOSApplication):
 *
 *     receiverDrawerComponent: Component {
 *         Common.IQRxDrawer { controller: iqcontrol }
 *     }
 */
Drawer {
	id: root

	property int headerHeight: 0
	// IQController backend (context property provided by the application)
	property var controller: null

	// Match app-wide Material settings (same conventions as PoolDrawer)
	Material.theme: Material.Dark
	Material.primary: "#227b3d"
	Material.accent: "#227b3d"
	Material.roundedScale: Material.notRounded

	implicitHeight: parent ? parent.height - headerHeight : 0
	y: headerHeight
	implicitWidth: 420
	edge: Qt.LeftEdge
	dragMargin: 50
	modal: false

	background: Rectangle {
		radius: 0
		color: "#222a2f"
	}

	ScrollView {
		id: scrollView
		anchors.fill: parent
		clip: true
		// pin content to the viewport width so nothing can scroll sideways
		contentWidth: availableWidth
		ScrollBar.vertical.visible: true
		ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
		anchors.leftMargin: 20
		anchors.rightMargin: 20

		ColumnLayout {
			width: scrollView.availableWidth

			Label {
				Layout.fillWidth: true
				text: "IQ Capture Settings"
				font.pixelSize: 18
				color: "#ffffff"
				horizontalAlignment: Text.AlignHCenter
				topPadding: 20
				bottomPadding: 8
			}

			Common.IQSettings {
				id: iqSettings
				controller: root.controller
				onControllerChanged: loadFromController()
				Layout.fillWidth: true
			}

			Rectangle {
				Layout.fillWidth: true
				Layout.topMargin: 12
				Layout.bottomMargin: 4
				height: 1
				color: "#3a454c"
			}

			Common.ReftxToneSettings {
				id: reftxToneSettings
				controller: root.controller
				Layout.fillWidth: true
				Layout.bottomMargin: 16
			}
		}
	}
}
