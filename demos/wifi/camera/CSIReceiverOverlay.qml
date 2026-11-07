import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
	/* Statistics display in bottom right corner */
	Rectangle {
		id: statsRectangle

		anchors.bottom: parent.bottom
		anchors.left: parent.left
		anchors.leftMargin: 20
		anchors.bottomMargin: 60
		width: 180
		height: 80
		color: "black"
		opacity: 0.8
		radius: 10

		Text {
			id: statsText
			text: "<b>Statistics</b><br/>Power: " + (isFinite(overlayModel.receiverPower) ? overlayModel.receiverPower.toFixed(2) + " " + overlayModel.receiverPowerUnit : "No Data") + "<br/>Antennas: " + overlayModel.activeAntennas.toFixed(1)
			color: "white"
			font.family: "Monospace"
			font.pixelSize: 16
			anchors.top: parent.top
			anchors.left: parent.left
			anchors.topMargin: 10
			anchors.leftMargin: 10
		}
	}

	/* List of transmitter MACs in top right corner if enabled */
	Rectangle {
		id: macsListRectangle

		anchors.top: parent.top
		anchors.right: parent.right
		anchors.rightMargin: 10
		anchors.topMargin: 10
		width: 220
		height: 200
		color: "black"
		opacity: 0.8
		radius: 10
		visible: overlayModel.macListEnabled

		ListModel {
			id: transmitterListModel
		}

		Item {
    		width: parent.width
    		height: parent.height
			Layout.fillWidth: true
			Layout.fillHeight: true
			anchors.fill: parent

			Component {
				id: transmitterListDelegate
				Item {
					width: 200
					height: 30
					anchors.margins: 5
					RowLayout {
						CheckBox {
							checked: visibilityChecked
							indicator.width: 18
							indicator.height: 18

							onCheckedChanged: function() {
								if (checked) {
									backend.setMacFilter(mac)
								} else {
									backend.clearMacFilter()
								}
								visibilityChecked = checked
								updateTransmitterList()
							}
						}
						Column {
							Text {
								text: "<b>MAC:</b> " + mac
								color: "#ffffff"
							}
						}
					}
				}
			}

			ListView {
				id: transmitterList
				anchors.fill: parent
				model: transmitterListModel
				delegate: transmitterListDelegate
			}
		}
	}

	function updateTransmitterList() {
		// Delete transmitters that should no longer be there
		let marked_for_removal = []
		let existing_macs = []
		let macFilterEnabled = false
		for (let i = 0; i < transmitterListModel.count; ++i) {
			let mac = transmitterListModel.get(i).mac
			if (transmitterListModel.get(i).visibilityChecked) {
				macFilterEnabled = true
			}
			if (!overlayModel.macList.includes(mac)) {
				marked_for_removal.push(i)
			} else {
				existing_macs.push(mac)
			}
		}

		// If at least one MAC filter is enabled, mark all other MACs for removal
		if (macFilterEnabled) {
			for (let i = 0; i < transmitterListModel.count; ++i) {
				let mac = transmitterListModel.get(i).mac
				if (!transmitterListModel.get(i).visibilityChecked)
					marked_for_removal.push(i)
			}
		}

		for (const i of marked_for_removal.reverse()) {
			// Do not remove the item if it is the currently selected MAC filter
			if (!transmitterListModel.get(i).visibilityChecked) {
				transmitterListModel.remove(i)
			}
		}

		// Add transmitters that have appeared unless MAC filter is enabled
		if (!macFilterEnabled) {
			for (let i = 0; i < overlayModel.macList.length; ++i) {
				let mac = overlayModel.macList[i]
				if (!existing_macs.includes(mac))
					transmitterListModel.append({"mac" : mac, "visibilityChecked" : false})
			}
		}
	}

    Connections { target: overlayModel; function onMacListChanged(addresses) { updateTransmitterList() } }
}
