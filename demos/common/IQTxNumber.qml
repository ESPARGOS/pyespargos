import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material

TextField {
    id: root
    property real value: 0
    property real minimum: -1e9
    property real maximum: 1e9
    property int decimals: 3
    signal edited(real number)
    implicitWidth: 100
    selectByMouse: true
    horizontalAlignment: TextInput.AlignRight
    validator: DoubleValidator { bottom: root.minimum; top: root.maximum; decimals: root.decimals; locale: "C" }
    function refresh() { if (!activeFocus) text = Number(value.toFixed(decimals)).toString() }
    Component.onCompleted: refresh()
    onValueChanged: refresh()
    onActiveFocusChanged: refresh()
    // Commit valid input immediately. A later steering click cannot undo it.
    onTextEdited: { if (acceptableInput) edited(Number(text)) }
    onEditingFinished: { if (!acceptableInput) text = Number(value.toFixed(decimals)).toString() }
}
