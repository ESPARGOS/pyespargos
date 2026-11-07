import QtQuick
import ".." as Common

Common.GainSlider {
    valueText: Number(value.toFixed(2)).toString()
}
