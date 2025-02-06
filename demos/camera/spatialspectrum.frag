#version 450

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 1) in vec4 vColor;
layout(location = 0) out vec4 fragmentColor;
layout(binding=1) uniform sampler2D cameraImage;

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
	bool fftMode;
	bool rawBeamspace;
	vec2 fov;
};

void main() {
	// flip camera image
	vec2 sourceCoord = vec2(1 - qt_TexCoord0.x, qt_TexCoord0.y);

	vec4 s = texture(cameraImage, sourceCoord);

	float gray = dot(s.rgb, vec3(0.21, 0.71, 0.07));

	if (rawBeamspace)
		fragmentColor = vColor;
	else
		fragmentColor = vec4(gray * 0.25 + 0.25 * s.r, gray * 0.25 + 0.25 * s.g, gray * 0.25 + 0.25 * s.b, s.a) + vColor;
}
