import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../common" as Common
import "../common" as BLE

Common.ESPARGOSApplication {
    id: window
    title: "BLE Phases by Antenna"
    minimumWidth: 800
    minimumHeight: 500
    color: "#11191e"
    receiverDrawerComponent: Component {
        BLE.BLERxDrawer {
            receiverConfig: bleController.receiverConfig
            busy: bleController.busy || backend.initializing
            calibrating: bleController.calibrating
            needsCalibration: bleController.needsCalibration
            calibrationSupported: bleController.calibrationSupported
            statusMessage: bleController.status
            onConfigurationRequested: settings => bleController.configureReceiver(settings)
            onCalibrationRequested: bleController.calibrate()
        }
    }
    appDrawerComponent: Component {
        Common.AppDrawer {
            id: settings
            title: "Settings"
            endpoint: appconfig
            Label { text: "Calibration" }
            Switch {
                property string configKey: "calibrated"
                property string configProp: "checked"
                Component.onCompleted: settings.configManager.register(this)
                onCheckedChanged: settings.configManager.onControlChanged(this)
            }
        }
    }
    Common.PhaseArrays {
        id: antennaArray
        anchors.fill: parent
        rowCount: backend.numberOfRows
        columns: backend.numberOfColumns
    }
    BLE.AdvertiserOverlay {
        advertiserModel: advertisers
        onSelectionRequested: (identity, selected) => {
            if (selected) advertisers.selectOnly(identity)
            else advertisers.select(identity, false)
        }
    }
    Timer {
        interval: 33
        running: true
        repeat: true
        onTriggered: backend.updateRequest()
    }
    Connections {
        target: backend
        function onUpdatePhases(panels) { antennaArray.panels = panels }
    }
}
