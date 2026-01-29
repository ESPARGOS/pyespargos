#version 450

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 1) in vec4 vColor;
layout(location = 0) out vec4 fragmentColor;
layout(binding=1) uniform sampler2D cameraImage;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;

	bool rawBeamspace;
	bool flip;
	vec2 fov;
};

// Converts azimuth and elevation angles (in radians) back into camera projection coordinates.
vec2 anglesToCameraPixel(vec2 angles) {
	vec2 halfFov = radians(fov) / 2;
	return 0.5 + 0.5 * tan(angles) / tan(halfFov);
}

// Converts FFT beamspace coordinates (ranging from -0.5 to 0.5) back into azimuth/elevation angles (radians).
vec2 FFTBeamspaceToAngles(vec2 beamspace) {
	vec2 b = 2.0 * beamspace;
	float sinEl = clamp(b.y, -1.0, 1.0);
	float el = asin(sinEl);
	float cosEl = max(cos(el), 1e-6);
	float sinAz = clamp(b.x / cosEl, -1.0, 1.0);
	float az = asin(sinAz);
	return vec2(az, el);
}

void main() {
	vec2 sourceCoord = vec2(flip ? qt_TexCoord0.x : 1 - qt_TexCoord0.x, qt_TexCoord0.y);
	vec2 sourceCoordBeamspace = anglesToCameraPixel(FFTBeamspaceToAngles(sourceCoord - 0.5));

	vec4 s = texture(cameraImage, rawBeamspace ? sourceCoordBeamspace : sourceCoord);

	if (rawBeamspace && (sourceCoordBeamspace.x < 0 || sourceCoordBeamspace.x > 1 || sourceCoordBeamspace.y < 0 || sourceCoordBeamspace.y > 1)) {
		s = vec4(0.0);
	}

	float gray = dot(s.rgb, vec3(0.21, 0.71, 0.07));

	fragmentColor = vec4(gray * 0.25 + 0.6 * s.r, gray * 0.25 + 0.6 * s.g, gray * 0.25 + 0.6 * s.b, s.a) + vColor;
}
