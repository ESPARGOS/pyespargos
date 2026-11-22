import QtQuick
import "../../wifi/camera" as CSI
import "../common" as WiFiIQ

CSI.CSIWindow {
    title: "ESPARGOS WiFi IQ Camera"
    dualFeed: false
    selectablePreamble: false
    receiverDrawerComponent: Component { WiFiIQ.ReceiverDrawer {} }
}
