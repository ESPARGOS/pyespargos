#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 qt_TexCoord0;
layout(location = 1) out vec4 beamspaceColor;
layout(location = 2) out vec4 beamspacePolarization;
layout(binding = 0) uniform sampler2D spatialSpectrumCanvasSource;
layout(binding = 1) uniform sampler2D polarizationCanvasSource;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;

	bool rawBeamspace;
	bool flip;
	vec2 fov;
	float time;
};

// Converts cartesian coordinates of the camera projection into a pair of azimuth and elevation angle (in radians).
vec2 cameraPixelToAngles(vec2 projection) {
	return atan(2 * (projection - 0.5) * tan(radians(fov) / 2));
	//return (projection - 0.5) * radians(fov);
}

// Converts azimuth and elevation angles (in radians) into FFT beamspace coordinates (ranging from -0.5 to 0.5).
vec2 anglesToFFTBeamspace(vec2 angles) {
	return 0.5 * vec2(cos(angles.y) * sin(angles.x), sin(angles.y));
}

void main() {
	vec2 coord = vec2(flip ? 1.0 - qt_MultiTexCoord0.x : qt_MultiTexCoord0.x, qt_MultiTexCoord0.y);

	vec2 angles = cameraPixelToAngles(coord);
	vec2 textureCoords = rawBeamspace ? coord : (anglesToFFTBeamspace(angles) + 0.5);

	beamspaceColor = texture(spatialSpectrumCanvasSource, textureCoords);
	beamspacePolarization = texture(polarizationCanvasSource, textureCoords);

    gl_Position = qt_Matrix * qt_Vertex;
    qt_TexCoord0 = qt_MultiTexCoord0;
}