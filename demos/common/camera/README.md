# Shared camera display

WiFi, BLE and IQ camera demos compose these components:

- `CameraView` owns video input, format selection, field of view and flips.
- `CameraOverlayModel` exposes projection, textures and markers to QML.
- `OverlayImages` delivers owned RGBA images safely to the render thread.
- `CameraOverlay.qml` combines video and the spatial-spectrum shader.
- `CameraSettings.qml` supplies the common camera and alignment controls.

Radio discovery, calibration, packet selection and beamforming stay in the
receiver or demo. Run `compile_shader.sh` or `compile_shader.bat` after GLSL
changes. The compiled shaders are included in this directory.
