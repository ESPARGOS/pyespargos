#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 qt_TexCoord0;
layout(location = 1) out vec4 vColor;
layout(binding = 0) uniform sampler2D spatialSpectrumCanvasSource;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;

	bool musicMode;
	bool fftMode;
	bool rawBeamspace;
	vec2 fov;
};

// Converts cartesian coordinates of the camera projection into a pair of azimuth and elevation angle (in radians).
vec2 toAngles(vec2 projection) {
	return atan(2 * (projection - 0.5) * tan(radians(fov) / 2));
	//return (projection - 0.5) * radians(fov);
}

vec2 toFFTBeamspace(vec2 angles) {
	return 0.5 * vec2(cos(angles.y) * sin(angles.x), sin(angles.y));
}

void main() {
	const int spectrumResolution = 8 * 16;

	vec2 angles = toAngles(qt_MultiTexCoord0);
	vec2 textureCoords = rawBeamspace ? qt_MultiTexCoord0 : (fftMode ? (toFFTBeamspace(angles) + 0.5) : ((degrees(angles) + 90) / 180));

	vec4 spatialSpectrumPixel = texture(spatialSpectrumCanvasSource, textureCoords);
	vColor = spatialSpectrumPixel;

    gl_Position = qt_Matrix * qt_Vertex;
    qt_TexCoord0 = qt_MultiTexCoord0;
}