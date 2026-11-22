import QtQuick
import "../common" as WiFi

CSIWindow {
    title: "ESPARGOS WiFi Camera"
    receiverDrawerComponent: Component { WiFi.WiFiTransceiverDrawer {} }
}
