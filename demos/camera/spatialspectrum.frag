#version 450

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 1) in vec4 beamspaceColor;
layout(location = 2) in vec4 beamspacePolarization;
layout(location = 0) out vec4 fragmentColor;
layout(binding=2) uniform sampler2D cameraImage;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;

	bool rawBeamspace;
	bool flip;
	vec2 fov;
	float time;
	bool polarizationVisible;
	float gridSpacing;
};

// Constants for polarization mode
float pol_oscillation_freq = 2.0;  // Frequency of polarization oscillation
float pointRadius = gridSpacing / 8.0;           // Radius of the polarization points in pixels

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

	// Decode polarization from texture
	// R = V real part, encoded as [0,1] -> [-1,1] (signed: preserves polarity between sources)
	// G = H real part, encoded as [0,1] -> [-1,1]
	// B = H imag part, encoded as [0,1] -> [-1,1]
	// A = 1.0 (opaque)
	// Power scaling is already baked into R/G/B by Python when normalization is off.
	float v_re = beamspacePolarization.r * 2.0 - 1.0;
	float h_re = beamspacePolarization.g * 2.0 - 1.0;
	float h_im = beamspacePolarization.b * 2.0 - 1.0;

	// Compute instantaneous E-field: Re(polarization * e^{j*omega*t})
	// This traces out the polarization ellipse over time
	// V is predominantly real after global phase normalization (can be negative for
	// sources with opposite sign), so V contributes only via cos(t)
	float ct = cos(pol_oscillation_freq * time);
	float st = sin(pol_oscillation_freq * time);
	float ev = v_re * ct;                // vertical displacement (V is signed real)
	float eh = h_re * ct - h_im * st;    // horizontal displacement

	// Displace each grid point by the polarization ellipse position
	// Check all 4 neighboring grid centers to handle large displacements crossing cell boundaries
	float amplitude = gridSpacing * 0.5;
	vec2 baseCell = floor(gl_FragCoord.xy / gridSpacing);
	float minDist = 1e6;
	for (int dy = 0; dy <= 1; dy++) {
		for (int dx = 0; dx <= 1; dx++) {
			vec2 gridCenter = (baseCell + vec2(dx, dy)) * gridSpacing;
			vec2 displacedCenter = gridCenter + vec2(eh, ev) * amplitude;
			float d = length(gl_FragCoord.xy - displacedCenter);
			minDist = min(minDist, d);
		}
	}

	// Compute polarization ellipse trace:
	// Sample the full ellipse path and find, for each pixel, the best contribution
	// considering both spatial proximity and temporal fading (points further "behind"
	// the current dot position fade out, creating a comet-tail effect).
	float currentPhase = mod(pol_oscillation_freq * time, 2.0 * 3.14159265);
	float traceContribution = 0.0;
	float traceWidth = 0.5 * pointRadius; // width of the trace in pixels
	int numSamples = 48;
	for (int i = 0; i < numSamples; i++) {
		float phase = float(i) / float(numSamples) * 2.0 * 3.14159265;

		// Temporal fade: how far behind the current dot position is this sample?
		// phaseDist = 0 at current position, increases going backwards
		float phaseDist = mod(currentPhase - phase, 2.0 * 3.14159265);
		float timeFade = exp(-phaseDist * 0.6);

		float ct_sample = cos(phase);
		float st_sample = sin(phase);
		float ev_sample = v_re * ct_sample;
		float eh_sample = h_re * ct_sample - h_im * st_sample;

		for (int dy = 0; dy <= 1; dy++) {
			for (int dx = 0; dx <= 1; dx++) {
				vec2 gridCenter = (baseCell + vec2(dx, dy)) * gridSpacing;
				vec2 tracePoint = gridCenter + vec2(eh_sample, ev_sample) * amplitude;
				float d = length(gl_FragCoord.xy - tracePoint);
				float spatialFade = smoothstep(traceWidth + 1.0, 0.0, d);
				traceContribution = max(traceContribution, spatialFade * timeFade);
			}
		}
	}

	float glowContribution = 0.4;
	float pointContribution = minDist < pointRadius ? (pointRadius - minDist) / pointRadius : glowContribution;
	// Combine the dot and trace, trace is dimmer than the dot itself
	float totalContribution = max(pointContribution, traceContribution * 0.6 + glowContribution);

	// If not in polarization "show" mode, skip dot/trace overlay
	if (!polarizationVisible)
		totalContribution = 1.0;

	fragmentColor = vec4(gray * 0.25 + 0.5 * s.r, gray * 0.25 + 0.5 * s.g, gray * 0.25 + 0.5 * s.b, s.a) + beamspaceColor * totalContribution;
}
