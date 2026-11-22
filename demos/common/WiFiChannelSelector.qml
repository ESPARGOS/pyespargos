import QtQuick
import QtQuick.Controls

ComboBox {
    property int channelCount: 13
    implicitWidth: 180
    model: Array.from({length: channelCount}, (_, i) => {
        const mhz = i === 13 ? 2484 : 2412 + 5 * i
        return (i + 1) + " (" + (mhz / 1000).toFixed(3) + " GHz)"
    })
    function isUserActive() { return pressed || popup.visible }
}
