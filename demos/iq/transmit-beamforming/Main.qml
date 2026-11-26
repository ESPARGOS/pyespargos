import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../common" as Common

Common.ESPARGOSApplication {
    id: window
    title: "IQ Transmit Beamforming"
    width: 1440
    height: 960
    minimumWidth: 720
    minimumHeight: 640
    receiverDrawerLabel: "⚙ TX"
    onClosing: event => { event.accepted = false; txcontrol.close() }
    Connections {
        target: txcontrol
        function onError(message) { window.showError("IQ transmission",message) }
    }
    receiverDrawerComponent: Component { IQTxDrawer { controller: txcontrol } }
    appDrawerComponent: Component {
        Common.AppDrawer {
            title: "Display & monitor"
            endpoint: appconfig
            contentLayout.width: Math.max(0,width-40)
            IQTxMonitorSettings { controller: txcontrol; Layout.columnSpan: 2; Layout.fillWidth: true }
        }
    }
    calibrationMessage: txcontrol.state.calibrating && !txcontrol.state.closing ? txcontrol.state.message : ""
    ScrollView {
        id: viewport
        clip: true
        contentWidth: availableWidth
        anchors.fill: parent
        anchors.margins: 24
        anchors.bottomMargin: 60
        IQTxWorkspace {
            width: Math.min(1600, viewport.availableWidth)
            x: (viewport.availableWidth - width) / 2
            height: Math.max(implicitHeight, viewport.availableHeight)
            controller: txcontrol
        }
    }
}
