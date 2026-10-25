#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 qt_TexCoord0;
layout(location = 1) out vec4 displayScale;
layout(location = 2) out vec2 powerScale;
layout(location = 3) out vec4 texelScale;
layout(binding = 1) uniform sampler2D source;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float floorDb;
    float spanDb;
    float xLow;
    float xHigh;
    float legend;
    float polar;
    float linearScale;
    float beamspacePeriod;
};

void main() {
    qt_TexCoord0 = qt_MultiTexCoord0;
    if (legend < 0.5) {
        qt_TexCoord0 = vec2(mix(xLow, xHigh, qt_MultiTexCoord0.x), 1.0 - qt_MultiTexCoord0.y);
        if (polar > 0.5)
            qt_TexCoord0.x = 2.0 * qt_MultiTexCoord0.x - 1.0;
    }
    // These depend only on display settings, not the sampled radar power.
    displayScale = vec4(vec2(65280.0, 255.0) * (128.0 / 65535.0) / spanDb,
                        (-32.0 - floorDb) / spanDb, spanDb * (3.32192809489 / 10.0));
    float lower = exp2(-displayScale.w);
    powerScale = vec2(lower, 1.0 / (1.0 - lower));
    vec2 size = vec2(textureSize(source, 0));
    texelScale = vec4((size - 1.0) / size, 0.5 / size);
    gl_Position = qt_Matrix * qt_Vertex;
}
