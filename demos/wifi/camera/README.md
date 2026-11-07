# Shader Compilation

The camera demo uses fragment and vertex shaders, which must be available in compiled form (`.qsb` files). Pre-compiled shaders are shipped in this repository, so normally no additional steps are required.

If the demo does not work (e.g., shader-related errors on startup), try re-compiling the shaders yourself using the `compile_shader.sh` (Linux / macOS) or `compile_shader.bat` (Windows) scripts. This requires the Qt Shader Baker (`qsb`) to be installed, and you may need to adapt the path to `qsb` in the script.

Camera capture, settings, projection, marker support, image textures and shaders
are shared with the IQ and BLE cameras in `demos/common/camera`. The compilation scripts
here forward to that directory. WiFi decoding, CSI processing, MAC selection
and polarization estimation remain specific to this demo.

The shared camera displays the natural forward-facing image by default. Use the camera/overlay flip controls if a mirrored view is wanted. Field of
view and azimuth/elevation alignment controls are shared by all three demos.

Camera options use the standard configuration overrides:

```sh
-o app.camera.enable=false
-o app.receiver.csi_completion_timeout=0.2
-o 'app.receiver.additional_calibration="calibration.npy"'
```

`--no-camera` is also available as a shortcut for `-o app.camera.enable=false`.
