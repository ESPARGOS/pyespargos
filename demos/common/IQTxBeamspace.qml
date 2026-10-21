import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var controller
    required property int beamIndex
    signal beamSelected(int index)
    readonly property var colors: ["#70cf8e", "#60c7ef", "#ffa760", "#db97e5"]
    readonly property var beam: controller.state.beams[beamIndex]
    Canvas {
        id: plot
        objectName: "txBeamspace"
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.minimumHeight: 240
        onWidthChanged: requestPaint()
        Connections { target: root; function onBeamIndexChanged() { plot.requestPaint() } }
        onHeightChanged: requestPaint()
        Connections { target: root.controller; function onChanged() { plot.requestPaint() } }
        onPaint: {
            let c = getContext("2d"), r = Math.min(width,height)/2-25, cx=width/2, cy=height/2;
            c.reset(); c.fillStyle="#11191e"; c.fillRect(0,0,width,height);
            c.strokeStyle="#405159"; c.lineWidth=1;
            c.beginPath(); c.arc(cx,cy,r,0,2*Math.PI); c.stroke();
            for(let angle=-60;angle<=60;angle+=30) {
                let a=angle*Math.PI/180;
                c.beginPath();
                for(let e=-90;e<=90;e+=2) {
                    let x=cx+r*Math.sin(a)*Math.cos(e*Math.PI/180), y=cy-r*Math.sin(e*Math.PI/180);
                    if(e===-90)c.moveTo(x,y);else c.lineTo(x,y);
                }
                c.stroke();
                let y=cy-r*Math.sin(a), dx=r*Math.cos(a);
                c.beginPath(); c.moveTo(cx-dx,y); c.lineTo(cx+dx,y); c.stroke();
                c.fillStyle="#aab9c0"; c.font="12px sans-serif"; c.fillText(angle+"°",cx+r*Math.sin(a)+3,cy+15);
            }
            // Draw the selected beam last so coincident beams remain selectable.
            let order=[];
            for(let i=0;i<root.controller.state.count;i++) if(i!==root.beamIndex) order.push(i);
            order.push(root.beamIndex);
            for(let i of order) {
                let beam=root.controller.state.beams[i], x=cx+beam.u*r, y=cy-beam.v*r;
                c.fillStyle=root.colors[i]; c.beginPath(); c.arc(x,y,15,0,2*Math.PI); c.fill();
                if(i===root.beamIndex) { c.strokeStyle="white"; c.lineWidth=2; c.stroke(); }
                c.fillStyle="#11191e"; c.font="bold 14px sans-serif"; c.textAlign="center";
                c.fillText(String(i+1),x,y+4);
            }
        }
        MouseArea {
            anchors.fill: parent
            objectName: "txBeamDrag"
            preventStealing: true
            property int draggedBeam: -1
            function steer(x,y) {
                let r=Math.min(width,height)/2-25;
                if(draggedBeam>=0 && draggedBeam<root.controller.state.count)
                    root.controller.steer(draggedBeam,(x-width/2)/r,(height/2-y)/r);
            }
            onPressed: mouse => {
                forceActiveFocus();
                let r=Math.min(width,height)/2-25, best=22*22;
                draggedBeam=root.beamIndex;
                // Prefer the selected beam if handles overlap.
                let selected=root.beam, dx=mouse.x-width/2-selected.u*r, dy=mouse.y-height/2+selected.v*r;
                if(dx*dx+dy*dy>best) {
                    for(let i=0;i<root.controller.state.count;i++) {
                        let beam=root.controller.state.beams[i];
                        dx=mouse.x-width/2-beam.u*r; dy=mouse.y-height/2+beam.v*r;
                        let distance=dx*dx+dy*dy;
                        if(distance<best) { best=distance; draggedBeam=i; }
                    }
                }
                root.beamSelected(draggedBeam);
                steer(mouse.x,mouse.y);
            }
            onPositionChanged: mouse => { if(pressed) steer(mouse.x,mouse.y) }
            onReleased: draggedBeam=-1
            onCanceled: draggedBeam=-1
        }
    }
    Label {
        Layout.fillWidth: true
        horizontalAlignment: Text.AlignHCenter
        text: "Azimuth " + (Math.atan2(root.beam.u,Math.sqrt(Math.max(0,1-root.beam.u*root.beam.u-root.beam.v*root.beam.v)))*180/Math.PI).toFixed(1) + "° · elevation " + (Math.asin(root.beam.v)*180/Math.PI).toFixed(1) + "°"
    }
}
