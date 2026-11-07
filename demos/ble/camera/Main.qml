import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../common" as Common
import "../common" as BLE
import "../../common/camera" as Camera

Common.ESPARGOSApplication {
    id: window
    title: "ESPARGOS BLE Camera"
    minimumWidth: 800
    minimumHeight: 500
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
            id: drawer
            title: "App Settings"
            endpoint: appconfig
            implicitWidth: 360
            objectName: "bleAppDrawer"
            Common.BeamformingSettings {
                configManager: drawer.configManager
                backlogTitle: "BLE backlog"
                polarizationSupported: true
                busy: bleController.busy || backend.initializing
                onPolarizationRequested: mode => {
                    if (mode !== "ignore") bleController.configureReceiver({polarization: 4})
                }
            }
            Camera.CameraSettings { configManager: drawer.configManager }
        }
    }

    Camera.CameraOverlay {
        anchors.fill: parent

        BLE.AdvertiserOverlay {
            advertiserModel: advertisers
            onSelectionRequested: (identity, selected) => advertisers.select(identity, selected)
        }

    }
}
