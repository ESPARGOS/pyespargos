import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick.Dialogs
import ESPARGOS.Tx 1.0

GridLayout {
    id: root
    required property var controller
    property int beamIndex: 0
    readonly property var state: controller.state
    readonly property var beam: state.beams[beamIndex]
    onStateChanged: { if (beamIndex >= state.count) beamIndex = state.count-1 }
    columns: width < 900 ? 1 : 2
    columnSpacing: 24
    rowSpacing: 20
    Layout.minimumHeight: columns === 1 ? 800 : 440
    enabled: state.authorized && !state.exclusive && !state.closing
    ColumnLayout {
        Layout.preferredWidth: root.columns === 1 ? -1 : 440
        Layout.maximumWidth: root.columns === 1 ? Infinity : 480
        Layout.fillWidth: true
        Layout.fillHeight: true
        Label { text: "Waveform"; font.pixelSize: 18 }
        TabBar {
            Layout.fillWidth: true
            currentIndex: root.beamIndex
            Repeater {
                model: root.state.count
                delegate: TabButton {
                    required property int index
                    text: "Beam " + (index+1)
                    onClicked: root.beamIndex = index
                }
            }
        }
        GridLayout {
            Layout.fillWidth: true
            columns: 2
            columnSpacing: 16
            rowSpacing: 8
            Label { text: "Type" }
            ComboBox {
                Layout.fillWidth: true
                implicitHeight: 40
                textRole: "label"
                valueRole: "value"
                model: [
                    {label: "Complex tone (CW)", value: "cw"},
                    {label: "Two tones", value: "two-tone"},
                    {label: "Linear chirp (LFM)", value: "lfm"},
                    {label: "Random noise", value: "noise"},
                    {label: "QPSK", value: "qpsk"},
                    {label: "I/Q file", value: "file"}
                ]
                currentIndex: ["cw","two-tone","lfm","noise","qpsk","file"].indexOf(root.beam.type)
                onActivated: root.controller.setBeam(root.beamIndex,"type",currentValue)
            }
            Label { text: "Peak amplitude" }
            RowLayout {
                Layout.fillWidth: true
                Slider {
                    objectName: "txAmplitude"
                    Layout.fillWidth: true
                    from: 0
                    to: root.state.unlocked ? 1 : 0.4
                    stepSize: 0.001
                    value: root.beam.amplitude
                    onMoved: root.controller.setBeam(root.beamIndex,"amplitude",value)
                }
                Label { text: root.beam.amplitude.toFixed(3); Layout.preferredWidth: 48; horizontalAlignment: Text.AlignRight }
            }
            Label { text: "Offset (MHz)"; visible: root.beam.type === "cw" || root.beam.type === "two-tone" }
            IQTxNumber { Layout.fillWidth: true; implicitHeight: 40; visible: root.beam.type === "cw" || root.beam.type === "two-tone"; value: root.beam.offset_mhz; minimum: -40; maximum: 40; onEdited: number => root.controller.setBeam(root.beamIndex,"offset_mhz",number) }
            Label { text: "Sweep (MHz)"; visible: root.beam.type === "lfm" }
            RowLayout {
                visible: root.beam.type === "lfm"
                IQTxNumber { implicitHeight: 40; Layout.preferredWidth: 80; value: root.beam.start_mhz; minimum: -40; maximum: 40; onEdited: number => root.controller.setBeam(root.beamIndex,"start_mhz",number) }
                Label { text: "to" }
                IQTxNumber { implicitHeight: 40; Layout.preferredWidth: 80; value: root.beam.stop_mhz; minimum: -40; maximum: 40; onEdited: number => root.controller.setBeam(root.beamIndex,"stop_mhz",number) }
            }
            Label { text: "Duration (µs)"; visible: root.beam.type === "lfm" }
            IQTxNumber { Layout.fillWidth: true; implicitHeight: 40; visible: root.beam.type === "lfm"; value: root.beam.duration_us; minimum: 0.0125; maximum: 102.4; decimals: 4; onEdited: number => root.controller.setBeam(root.beamIndex,"duration_us",number) }
            Label { text: "Delay (µs)"; visible: root.beam.type === "lfm" }
            IQTxNumber { Layout.fillWidth: true; implicitHeight: 40; visible: root.beam.type === "lfm"; value: root.beam.delay_us; minimum: 0; maximum: 102.4; decimals: 4; onEdited: number => root.controller.setBeam(root.beamIndex,"delay_us",number) }
            Button { text: "Upload I/Q file"; visible: root.beam.type === "file"; Layout.columnSpan: 2; onClicked: fileDialog.open() }
            Label { text: root.beam.file.split('/').pop() || "Choose a file"; visible: root.beam.type === "file"; Layout.columnSpan: 2; elide: Text.ElideMiddle; Layout.maximumWidth: 330 }
        }
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 170
            color: "#11191e"
            border.color: "#405159"
            radius: 4
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 10
                RowLayout {
                    Label { text: "Waveform preview"; Layout.fillWidth: true }
                    Label { text: "I"; color: "#60c7ef" }
                    Label { text: "Q"; color: "#ffa760" }
                }
                IQTxPlot {
                    objectName: "txWaveformPreview"
                    controller: root.controller
                    beamIndex: root.beamIndex
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ToolTip.visible: previewHover.hovered
                    ToolTip.text: "Scroll to zoom at the pointer. Drag to pan. Shift + scroll zooms amplitude. Double-click to reset."
                    HoverHandler { id: previewHover }
                }
            }
        }
    }
    ColumnLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.minimumHeight: 360
        Label { text: "Steering"; font.pixelSize: 18 }
        IQTxBeamspace {
            controller: root.controller
            beamIndex: root.beamIndex
            onBeamSelected: index => root.beamIndex = index
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
    }
    FileDialog { id: fileDialog; title: "Complex I/Q samples"; nameFilters: ["I/Q samples (*.npy *.csv *.txt)"]; onAccepted: root.controller.setBeam(root.beamIndex,"file",selectedFile.toString()) }
}
