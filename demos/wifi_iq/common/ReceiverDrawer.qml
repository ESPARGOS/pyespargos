import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../../common" as Common

Drawer {
    id: drawer
    property int headerHeight: 0
    readonly property int rfSwitchMode: wifiIQControls.settings.rf_switch ?? -1
    implicitHeight: parent ? parent.height - headerHeight : 0
    implicitWidth: 400
    y: headerHeight
    edge: Qt.LeftEdge
    modal: false
    Material.theme: Material.Dark
    Material.primary: "#227b3d"
    Material.accent: Material.primary
    Material.roundedScale: Material.notRounded
    background: Rectangle { color: "#222a2f" }

    component Section: Label {
        Layout.columnSpan: 2
        color: "#9fb3c8"
    }

    component FieldLabel: Label {
        color: "#ffffff"
        horizontalAlignment: Text.AlignRight
        Layout.alignment: Qt.AlignRight
        Layout.fillWidth: true
    }

    function readSettings() {
        const cfg = wifiIQControls.settings
        if (cfg.channel === undefined) return
        channel.currentIndex = cfg.channel - 1
        gainMode.checked = cfg.gain_mode === "auto"
        if (!gain.pressed) gain.value = cfg.rx_gain
        feed.value = cfg.rf_switch
        shared.checked = cfg.boot_sync
        triggers.mask = cfg.boot_trigger_mask ?? 255
        rate.value = cfg.max_pps
        const words = Math.max(cfg.ofdm_words, cfg.dsss_words)
        capture.model = [...new Set([1024, 1536, 2048, 3072, 4096, words])].sort((a,b) => a-b)
        capture.currentIndex = capture.model.indexOf(words)
        filter.currentIndex = cfg.filter_bw_mhz === 255 ? 1 : cfg.filter_bw_mhz > 0 ? 2 : 0
        if (!bandwidth.pressed) bandwidth.value = filter.currentIndex === 2 ? cfg.filter_bw_mhz : 20
    }
    Component.onCompleted: readSettings()
    Connections {
        target: wifiIQControls
        function onSettingsChanged() { drawer.readSettings() }
    }

    ScrollView {
        id: scroll
        anchors.fill: parent
        anchors.leftMargin: 20
        anchors.rightMargin: 20
        contentWidth: availableWidth
        clip: true
        GridLayout {
            width: scroll.availableWidth
            columns: 2
            columnSpacing: 16
            rowSpacing: 10
            enabled: !backend.initializing
            Label { Layout.columnSpan: 2; text: "Receiver Settings"; font.pixelSize: 18; topPadding: 20; color: "#ffffff" }
            Section { text: "Channel" }
            FieldLabel { text: "Channel" }
            Common.WiFiChannelSelector { id: channel; channelCount: 14; onActivated: wifiIQControls.apply({channel: currentIndex + 1}) }
            Section { text: "Calibration" }
            Common.CalibrationButton {
                enabled: !wifiIQControls.busy
                needCalibration: !wifiIQControls.calibrated
                onClicked: wifiIQControls.calibrate()
            }
            ProgressBar {
                Layout.columnSpan: 2
                Layout.fillWidth: true
                visible: wifiIQControls.busy && wifiIQControls.status.length > 0
                indeterminate: visible
            }
            Label {
                Layout.columnSpan: 2
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: wifiIQControls.status
                visible: !wifiIQControls.busy && text.length > 0 && text !== "Calibrated" && text !== "Configured"
            }
            Section { text: "Signal Path / Format" }
            FieldLabel { text: "Training channel" }
            ComboBox {
                property string configKey: "space_time_stream"
                property string configProp: "currentIndex"
                model: ["Common preamble", ...Array.from({length: 8}, (_, i) => "Space-time stream " + (i + 1))]
                Component.onCompleted: csiconfig.register(this)
                onActivated: csiconfig.onControlChanged(this)
            }
            FieldLabel { text: "RF input" }
            Common.RFSwitchControl { id: feed; modes: [0, 2, 3, 4, 1]; onActivated: wifiIQControls.apply({rf_switch: value}) }
            FieldLabel { text: "Analog RX filter" }
            ComboBox { id: filter; onActivated: wifiIQControls.apply({filter_bw_mhz: [0, 255, bandwidth.value][currentIndex]}); model: ["Native", "Open", "Manual"]; enabled: wifiIQControls.settings.filter_bw_mhz !== undefined }
            FieldLabel { text: "Bandwidth"; visible: filter.currentIndex === 2 }
            Common.GainSlider {
                id: bandwidth
                onEdited: wifiIQControls.apply({filter_bw_mhz: value})
                Layout.fillWidth: true
                visible: filter.currentIndex === 2
                from: 13; to: 40; valueText: Math.round(value) + " MHz"
            }
            Section { text: "Gain" }
            FieldLabel { text: "Automatic" }
            Switch { id: gainMode; onToggled: wifiIQControls.apply({gain_mode: checked ? "auto" : "manual"}) }
            FieldLabel { text: "RX Gain" }
            Common.GainSlider {
                id: gain
                enabled: !gainMode.checked
                onValueChanged: if (pressed && !gainUpdate.running) gainUpdate.start()
                onEdited: { gainUpdate.stop(); wifiIQControls.apply({rx_gain: value}) }
                Timer { id: gainUpdate; interval: 100; onTriggered: wifiIQControls.apply({rx_gain: gain.value}) }
            }
            Section { text: "Packet capture" }
            FieldLabel { text: "Shared trigger" }
            Switch { id: shared; onToggled: wifiIQControls.apply({boot_sync: checked}) }
            FieldLabel { text: "Trigger sensors"; visible: triggers.visible }
            GridLayout {
                id: triggers
                property int mask: 255
                columns: 4
                columnSpacing: 0
                rowSpacing: 0
                visible: shared.checked && wifiIQControls.settings.boot_trigger_mask !== undefined
                Repeater {
                    model: 8
                    CheckBox {
                        required property int index
                        text: index.toString()
                        checked: Boolean(triggers.mask & (1 << index))
                        enabled: triggers.mask !== (1 << index)
                        onToggled: { triggers.mask ^= 1 << index; wifiIQControls.apply({boot_trigger_mask: triggers.mask}) }
                    }
                }
            }
            FieldLabel { text: "Capture window" }
            ComboBox {
                id: capture
                onActivated: wifiIQControls.apply({ofdm_words: currentValue, dsss_words: currentValue})
                displayText: currentValue === undefined ? "" : currentValue / 20 + " µs"
                delegate: ItemDelegate { required property int modelData; text: modelData / 20 + " µs"; width: capture.width }
            }
            FieldLabel { text: "Rate / sensor" }
            SpinBox { id: rate; onValueModified: wifiIQControls.apply({max_pps: value}); from: 1; to: 500; editable: true; Accessible.name: "Packets per second per sensor" }
            Section { text: "Channel estimation" }
            FieldLabel { text: "Waveform fields" }
            ComboBox {
                id: estimator
                readonly property var modes: ["training", "headers", "payload"]
                model: ["Training only", "Training + header", "Training + header + payload"]
                currentIndex: modes.indexOf(wifiIQControls.estimator)
                onActivated: wifiIQControls.setEstimator(modes[currentIndex])
            }
            Section { text: "Transmitter MAC filter"; visible: backend.hasBacklog }
            FieldLabel { text: "Enable Filter"; visible: backend.hasBacklog }
            Switch {
                visible: backend.hasBacklog
                enabled: checked || mac.acceptableInput
                checked: wifiIQControls.macFilter !== ""
                onClicked: wifiIQControls.setMacFilter(checked ? mac.text : "")
            }
            FieldLabel { text: "MAC Address"; visible: backend.hasBacklog }
            TextField {
                id: mac
                visible: backend.hasBacklog
                implicitWidth: 180
                placeholderText: "00:11:22:33:44:55"
                validator: RegularExpressionValidator { regularExpression: /[0-9A-Fa-f]{12}|([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}/ }
                onEditingFinished: if (wifiIQControls.macFilter && acceptableInput) wifiIQControls.setMacFilter(text)
                Connections {
                    target: wifiIQControls
                    function onFilterChanged() {
                        if (wifiIQControls.macFilter) mac.text = wifiIQControls.macFilter
                    }
                }
            }
        }
    }
}
