#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 qt_TexCoord0;
layout(location = 1) out vec4 vColor;
layout(binding = 0) uniform sampler2D spatialSpectrumCanvasSource;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;

	// Hack to get spatial spectra from QML to GLSL
	mat4 verticalSpatialSpectrum0;
	mat4 verticalSpatialSpectrum1;
	mat4 verticalSpatialSpectrum2;
	mat4 verticalSpatialSpectrum3;
	mat4 verticalSpatialSpectrum4;
	mat4 verticalSpatialSpectrum5;
	mat4 verticalSpatialSpectrum6;
	mat4 verticalSpatialSpectrum7;

	mat4 horizontalSpatialSpectrum0;
	mat4 horizontalSpatialSpectrum1;
	mat4 horizontalSpatialSpectrum2;
	mat4 horizontalSpatialSpectrum3;
	mat4 horizontalSpatialSpectrum4;
	mat4 horizontalSpatialSpectrum5;
	mat4 horizontalSpatialSpectrum6;
	mat4 horizontalSpatialSpectrum7;

	bool musicMode;
};

// Instensity value limits in dB
const float minIntensity = -10;
const float maxIntensity = 0;

// Map intensity values (in dB) to the range [0, 1], clip at lower bound
float normalizeIntensity(float db) {
	return max(0, (db - minIntensity) / (maxIntensity - minIntensity));
}

// FOV (field of view) of the camera
// TODO: Unify MUSIC and non-MUSIC modes... All of this FOV code is not really correct yet...
vec2 fov = musicMode ? radians(vec2(60.53481199242273, 37.46666255939329)) : radians(vec2(100.53481199242273, 37.46666255939329));
float center_z = 0.5 / tan(fov.x / 2);

// Converts a pair of azimuth and elevation angle (in radians) into cartesian coordinates of the camera projection.
// Moves the coordinate origin from the center to the top left corner, i.e. the visible area of the result is [0, 1] x [0, 1].
vec2 toProjectionCoordinates(vec2 angles) {
	return center_z * tan(angles) + 0.5;
}

// Converts cartesian coordinates of the camera projection into a pair of azimuth and elevation angle (in radians).
vec2 toAngles(vec2 projection) {
	vec2 anglesTan = (projection - 0.5) / center_z;

	vec2 angles = atan(anglesTan);

	return vec2(angles.x, angles.y);
}

void main() {
	const int spectrumResolution = 8 * 16;

	if (musicMode) {
		float verticalSpatialSpectrum[spectrumResolution];
		float horizontalSpatialSpectrum[spectrumResolution];

		for (int y = 0; y < 4; y++) {
			for (int x = 0; x < 4; x++) {
				verticalSpatialSpectrum[  0 + 4 * y + x] = verticalSpatialSpectrum0[x][y];
				verticalSpatialSpectrum[ 16 + 4 * y + x] = verticalSpatialSpectrum1[x][y];
				verticalSpatialSpectrum[ 32 + 4 * y + x] = verticalSpatialSpectrum2[x][y];
				verticalSpatialSpectrum[ 48 + 4 * y + x] = verticalSpatialSpectrum3[x][y];
				verticalSpatialSpectrum[ 64 + 4 * y + x] = verticalSpatialSpectrum4[x][y];
				verticalSpatialSpectrum[ 80 + 4 * y + x] = verticalSpatialSpectrum5[x][y];
				verticalSpatialSpectrum[ 96 + 4 * y + x] = verticalSpatialSpectrum6[x][y];
				verticalSpatialSpectrum[112 + 4 * y + x] = verticalSpatialSpectrum7[x][y];


				horizontalSpatialSpectrum[  0 + 4 * y + x] = horizontalSpatialSpectrum0[x][y];
				horizontalSpatialSpectrum[ 16 + 4 * y + x] = horizontalSpatialSpectrum1[x][y];
				horizontalSpatialSpectrum[ 32 + 4 * y + x] = horizontalSpatialSpectrum2[x][y];
				horizontalSpatialSpectrum[ 48 + 4 * y + x] = horizontalSpatialSpectrum3[x][y];
				horizontalSpatialSpectrum[ 64 + 4 * y + x] = horizontalSpatialSpectrum4[x][y];
				horizontalSpatialSpectrum[ 80 + 4 * y + x] = horizontalSpatialSpectrum5[x][y];
				horizontalSpatialSpectrum[ 96 + 4 * y + x] = horizontalSpatialSpectrum6[x][y];
				horizontalSpatialSpectrum[112 + 4 * y + x] = horizontalSpatialSpectrum7[x][y];
			}
		}

		vec2 angles = toAngles(qt_MultiTexCoord0);
		vec2 normalizedAngles = ((degrees(angles) + 90) / 180); // map [-90°, 90°] to [0, 1]
		vec2 spectrumIdx = normalizedAngles * spectrumResolution;
		ivec2 spectrumLeftIdx = ivec2(spectrumIdx);
		ivec2 spectrumRightIdx = ivec2(spectrumIdx) + 1;
		vec2 interpolation = spectrumIdx - spectrumLeftIdx;

		float horizontalIntensity = normalizeIntensity(
			(1 - interpolation.x) * horizontalSpatialSpectrum[spectrumLeftIdx.x]
			+ interpolation.x * horizontalSpatialSpectrum[spectrumRightIdx.x]
		);
		float verticalIntensity = normalizeIntensity(
			(1 - interpolation.y) * verticalSpatialSpectrum[spectrumLeftIdx.y]
			+ interpolation.y * verticalSpatialSpectrum[spectrumRightIdx.y]
		);

		// limit total intensity to prevent white flashes
		float intensity = min(0.8, (horizontalIntensity * verticalIntensity) * (horizontalIntensity * verticalIntensity));

		vColor = vec4(0.2 * intensity, intensity, 0.2 * intensity, 0.0);
	} else {
		vec2 angles = toAngles(qt_MultiTexCoord0);
		vec2 normalizedAngles = ((degrees(angles) + 90) / 180); // map [-90°, 90°] to [0, 1]
		vec2 spectrumIdx = normalizedAngles * spectrumResolution;

		vec4 spatialSpectrumPixel = texture(spatialSpectrumCanvasSource, normalizedAngles);
		vColor = spatialSpectrumPixel;
	}
	

    gl_Position = qt_Matrix * qt_Vertex;
    qt_TexCoord0 = qt_MultiTexCoord0;
}