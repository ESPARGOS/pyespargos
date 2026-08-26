#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 qt_TexCoord0;
layout(location = 1) out vec4 beamspaceColor;
layout(location = 2) out vec4 beamspacePolarization;
layout(binding = 1) uniform sampler2D spatialSpectrumCanvasSource;
layout(binding = 2) uniform sampler2D polarizationCanvasSource;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;

	int rawBeamspace;
	int flipCamera;
	int flipOverlay;
	vec2 fov;
	float time;
	int polarizationVisible;
	float gridSpacing;
	float azimuthCorrection;
	float elevationCorrection;
};

// Converts normalized camera coordinates into a unit-length ray in camera space.
vec3 cameraPixelToDirection(vec2 projection) {
	vec2 slopes = 2.0 * (projection - 0.5) * tan(radians(fov) / 2.0);
	return normalize(vec3(slopes, 1.0));
}

vec3 rotateElevation(vec3 direction, float angle) {
	float c = cos(angle);
	float s = sin(angle);
	return vec3(direction.x, c * direction.y + s * direction.z, -s * direction.y + c * direction.z);
}

vec3 rotateAzimuth(vec3 direction, float angle) {
	float c = cos(angle);
	float s = sin(angle);
	return vec3(c * direction.x + s * direction.z, direction.y, -s * direction.x + c * direction.z);
}

// Converts a camera-space direction into FFT beamspace coordinates (ranging from -0.5 to 0.5).
vec2 directionToFFTBeamspace(vec3 direction) {
	return 0.5 * direction.xy;
}

void main() {
	vec2 coord = vec2(flipOverlay == 1 ? 1.0 - qt_MultiTexCoord0.x : qt_MultiTexCoord0.x, qt_MultiTexCoord0.y);

	vec3 direction = cameraPixelToDirection(coord);
	direction = rotateElevation(direction, radians(elevationCorrection));
	direction = rotateAzimuth(direction, radians(azimuthCorrection));
	vec2 textureCoords = rawBeamspace == 1 ? coord : (directionToFFTBeamspace(direction) + 0.5);

	beamspaceColor = texture(spatialSpectrumCanvasSource, textureCoords);
	beamspacePolarization = texture(polarizationCanvasSource, textureCoords);

    gl_Position = qt_Matrix * qt_Vertex;
    qt_TexCoord0 = qt_MultiTexCoord0;
}
