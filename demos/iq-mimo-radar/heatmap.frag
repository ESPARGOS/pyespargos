#version 440
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 1) in vec4 displayScale;
layout(location = 2) in vec2 powerScale;
layout(location = 3) in vec4 texelScale;
layout(location = 0) out vec4 fragColor;
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

vec3 color(float t) {
    vec3 dark = vec3(0.067, 0.098, 0.118);
    vec3 blue = vec3(0.08, 0.30, 0.48);
    vec3 green = vec3(0.16, 0.72, 0.55);
    vec3 yellow = vec3(0.98, 0.87, 0.24);
    if (t < 0.3) return mix(dark, blue, t / 0.3);
    if (t < 0.7) return mix(blue, green, (t - 0.3) / 0.4);
    return mix(green, yellow, (t - 0.7) / 0.3);
}

void main() {
    float value;
    if (legend > 0.5) {
        value = qt_TexCoord0.x;
    } else {
        vec2 uv = qt_TexCoord0;
        if (polar > 0.5) {
            // Keep the nonlinear projection per pixel, including at the origin
            // and beamspace wrap. A mesh would only approximate these coordinates.
            float radius = length(uv);
            if (radius > 1.0) { fragColor = vec4(0.0); return; }
            float direction = radius > 0.0 ? uv.x / radius : 0.0;
            uv = vec2(fract(direction / beamspacePeriod + 0.5), radius);
        }
        uv = uv * texelScale.xy + texelScale.zw;
        vec3 encoded = texture(source, uv).rgb;
        value = dot(encoded.rg, displayScale.xy) + displayScale.z;
        value = clamp(value, 0.0, 1.0);
        if (linearScale > 0.5)
            value = (exp2((value - 1.0) * displayScale.w) - powerScale.x) * powerScale.y;
    }
    fragColor = vec4(color(value), 1.0) * qt_Opacity;
}
