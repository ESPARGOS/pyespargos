import QtQuick
import "../../wifi/common" as WiFi
import "../common" as WiFiIQ

WiFi.InstantaneousCSIWindow {
    selectablePreamble: false
    symbolPlot: Component { SymbolPlot {} }
    receiverDrawerComponent: Component { WiFiIQ.ReceiverDrawer {} }
}
