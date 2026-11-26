# 3D Radiation Pattern Demo

## Additional Requirements

Install the [standard demo dependencies](../../../README.md#installation). PyQt6 provides the `PyQt6.QtQuick3D` binding used to draw the radiation pattern. To display the antenna array, the demo loads `array-3dmodel.glb` with the `QtQuick3D.AssetUtils` QML module and Qt Quick 3D’s Assimp asset importer. [Qt RuntimeLoader documentation](https://doc.qt.io/qt-6/qml-qtquick3d-assetutils-runtimeloader.html)

The Linux PyPI wheels include the QML module and importer. If you use distribution-provided Qt instead, install the importer for that Qt installation:

```bash
# Debian / Ubuntu
sudo apt install qml6-module-quick3d-assetutils qt6-quick3d-assetimporters-plugin

# Fedora
sudo dnf install qt6-qtquick3d

# Arch Linux
sudo pacman -S qt6-quick3d
```
