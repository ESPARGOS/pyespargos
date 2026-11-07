import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

RowLayout {
    id: root
    property alias value: slider.value
    property alias from: slider.from
    property alias to: slider.to
    property alias stepSize: slider.stepSize
    readonly property alias pressed: slider.pressed
    property string toolTipText: ""
    property string valueText: Math.round(value).toString()
    readonly property bool hovered: hover.hovered
    signal edited()
    spacing: 8
    implicitWidth: 180
    function isUserActive() { return slider.pressed }
    ToolTip.visible: toolTipText.length > 0 && hover.hovered
    ToolTip.text: toolTipText
    HoverHandler { id: hover }
    Slider {
        id: slider
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        Layout.preferredWidth: 140
        from: 0
        to: 76
        stepSize: 1
        value: 32
        onPressedChanged: if (!pressed) root.edited()
        onMoved: if (!pressed) root.edited()
    }
    Label {
        text: root.valueText
        Layout.minimumWidth: Math.max(28, implicitWidth)
        horizontalAlignment: Text.AlignRight
        opacity: root.enabled ? 1 : 0.5
    }
}
