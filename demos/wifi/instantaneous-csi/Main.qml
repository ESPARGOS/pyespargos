import QtQuick
import "../common" as WiFi

WiFi.InstantaneousCSIWindow {
    receiverDrawerComponent: Component { WiFi.WiFiTransceiverDrawer {} }
}
