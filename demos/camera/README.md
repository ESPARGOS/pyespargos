# Shader Compilation

The camera demo uses fragment and vertex shaders, which must be available in compiled form (`.qsb` files). Pre-compiled shaders are shipped in this repository, so normally no additional steps are required.

If the demo does not work (e.g., shader-related errors on startup), try re-compiling the shaders yourself using the `compile_shader.sh` (Linux / macOS) or `compile_shader.bat` (Windows) scripts. This requires the Qt Shader Baker (`qsb`) to be installed, and you may need to adapt the path to `qsb` in the script.
