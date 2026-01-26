#version 450

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 1) in vec4 vColor;
layout(location = 0) out vec4 fragmentColor;
layout(binding=1) uniform sampler2D cameraImage;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;

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
		fragmentColor = vec4(gray * 0.25 + 0.6 * s.r, gray * 0.25 + 0.6 * s.g, gray * 0.25 + 0.6 * s.b, s.a) + vColor;
}
