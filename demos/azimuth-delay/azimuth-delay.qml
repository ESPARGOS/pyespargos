import QtQuick
import QtQuick.Window
import QtQuick.Shapes

Window {
    width: 1600
    height: 800
    visible: true
    color: "lightgray"

Rectangle{
    id:plotArea
    height:0.8*Math.min(parent.height, parent.width*0.4)
    width: 2*height
    anchors.centerIn: parent
    color:"lightgray"
    property color lineColor: Qt.rgba(0.3,0.3,0.3,0.9)
    property real lineWidth: 2.5

    ShaderEffect{
        id: shader
        anchors.fill: parent
        Canvas {
            id: plotCanvas
            width: angle_size
            height: delay_size
            property var imageData: undefined
            function createImageData() {
                const ctx = plotCanvas.getContext("2d");
                imageData = ctx.createImageData(width, height);
            }
            
            onAvailableChanged: if(available) createImageData();

            onPaint: {
                if(plotCanvas.imageData){
                    const ctx = plotCanvas.getContext("2d");
                    ctx.drawImage(plotCanvas.imageData, 0, 0);
                }
            }

        }
        property variant source: ShaderEffectSource {
            sourceItem: plotCanvas
            hideSource:true
            smooth:true
        }

        vertexShader: "vertex_shader.qsb"
        fragmentShader: "fragment_shader.qsb"
    }
}

    Canvas {
        id: canvas
        anchors.fill: parent        
        property var origin_x:0.5*canvas.width
        property var origin_y:0.5*(canvas.height-plotArea.height)+plotArea.height
        property var phi_num: 6
        property var text_color: "black"
        property var text_width: 1
        property var text_font: "14px sans-serif"
        property var delay_min: Delay_min
        property var delay_max: Delay_max
        property var num_delay: delay_max-delay_min

        onPaint: {
            var context = getContext("2d");
            context.font = text_font
            context.fillStyle = text_color;
            context.lineWidth = text_width;
            context.textAlign = "right";
            context.textBaseline = "top";
            context.fillText(delay_min, origin_x, origin_y+5)
            for(let i = 0; i<num_delay; i++){
                context.beginPath()
                context.strokeStyle = plotArea.lineColor;
                context.lineWidth = plotArea.lineWidth;
                context.arc(origin_x, origin_y, (1-(1/num_delay)*i)*plotArea.height,Math.PI,2*Math.PI);
                context.stroke();
                context.fillStyle = text_color;
                context.lineWidth = text_width;
                context.textAlign = "right";
                context.textBaseline = "top";
                context.fillText(((delay_max-i)), origin_x+(1-(1/num_delay)*i)*plotArea.height, origin_y+5);
            }
            for(let i =-phi_num/2; i<=phi_num/2; i++){
                context.beginPath()
                context.strokeStyle = plotArea.lineColor;
                context.lineWidth = plotArea.lineWidth;
                context.moveTo(origin_x, origin_y);
                context.lineTo(origin_x+Math.sin(i/phi_num*Math.PI)*plotArea.height, origin_y-Math.cos(i/phi_num*Math.PI)*plotArea.height);
                context.stroke();
                if(i<0){
                    context.fillStyle = text_color;
                    context.lineWidth = text_width;
                    context.textAlign = "right";
                    context.textBaseline = "bottom";
                    context.fillText(180/phi_num*(-i)+"°", origin_x+Math.sin(i/phi_num*Math.PI)*(plotArea.height+5), origin_y-Math.cos(i/phi_num*Math.PI)*(plotArea.height+5));
                }
                else if(i == 0){
                    context.fillStyle = text_color;
                    context.lineWidth = text_width;
                    context.textAlign = "center";
                    context.textBaseline = "bottom";
                    context.fillText(0+"°", origin_x+Math.sin(i/phi_num*Math.PI)*(plotArea.height+5), origin_y-Math.cos(i/phi_num*Math.PI)*(plotArea.height+5));
                }
                else{
                    context.fillStyle = text_color;
                    context.lineWidth = text_width;
                    context.textAlign = "left";
                    context.textBaseline = "bottom";
                    context.fillText(180/phi_num*(-i)+"°", origin_x+Math.sin(i/phi_num*Math.PI)*(plotArea.height+5), origin_y-Math.cos(i/phi_num*Math.PI)*(plotArea.height+5));
                }
            }
        }

    }

    Timer{
        interval: 1000/60
        running: true
        repeat: true
        onTriggered: {
        backend.update_data();
        }
    }

    Connections {
        target: backend
        function onDataChanged(image_data) {
            if (plotCanvas.imageData === undefined){
                const ctx = plotCanvas.getContext("2d")
                plotCanvas.imageData = ctx.createImageData(angle_size, delay_size)
            }
            let len = plotCanvas.imageData.data.length;
            for (let i = 0; i < len; i++){
                plotCanvas.imageData.data[i] = image_data[i];
            }
            plotCanvas.requestPaint();
        }
    }

}

