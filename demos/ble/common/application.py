"""Qt application lifecycle for BLE packet capture demos."""

from demos.common.application import ESPARGOSApplication


class ESPARGOSBluetoothApplication(ESPARGOSApplication):
    """Start BLE acquisition on the controller worker after validating the layout."""

    CONTROLLER_MODE = SENSOR_MODE = "ble"

    def _open_pool(self):
        self._prepare_pool_init({})
        if not hasattr(self, "pool"):
            self.pool = self._make_pool()
        if len(self.pool.hosts) != 1:
            raise ValueError("BLE demos currently support one eight-sensor board")
        return self.pool

    def exec(self):
        self.controller.start(self._open_pool)
        try:
            return super().exec()
        finally:
            self.onAboutToQuit()
