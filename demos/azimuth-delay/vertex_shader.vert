#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 qt_TexCoord0;
layout(location = 1) out vec4 vColor;
layout(binding = 1) uniform sampler2D source;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
};

void main() {
    gl_Position = qt_Matrix * qt_Vertex;
    vec2 position = vec2((2*qt_MultiTexCoord0.x-1), (1-qt_MultiTexCoord0.y));
    qt_TexCoord0 = position;
    vec4 pixel = texture(source, qt_MultiTexCoord0);
    vColor = pixel;
}
