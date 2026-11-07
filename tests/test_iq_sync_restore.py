import unittest

import numpy as np

from espargos import RFSwitchState
from espargos.iq_sync import acquire_reference_anchors


class _Revision:
    @staticmethod
    def antenna_id_to_row_col(antenna_id):
        return divmod(antenna_id, 4)


class _Board:
    revision = _Revision()


class _Cluster:
    sensor_timestamps = np.arange(8, dtype=float).reshape(1, 2, 4) / 1e9


class _Pool:
    def __init__(self, deliver):
        self.deliver = deliver
        self.callback = None
        self.emitting = False
        self.original_switch = RFSwitchState.SENSOR_RFSWITCH_ANTENNA_L
        self.switch_calls = []
        self.removed = None

    def add_callback(self, callback):
        self.callback = callback
        return object()

    def remove_callback(self, handle):
        self.removed = handle

    @property
    def emit_calibration_csi(self):
        return self.emitting

    @emit_calibration_csi.setter
    def emit_calibration_csi(self, enabled):
        self.emitting = bool(enabled)

    def get_rf_switch(self):
        return self.original_switch

    def set_rf_switch(self, state):
        self.switch_calls.append(state)

    def run(self):
        if self.deliver:
            self.deliver = False
            self.callback(_Cluster())


class IQSyncRestoreTests(unittest.TestCase):
    def test_success_restores_wifi_rf_switch(self):
        pool = _Pool(deliver=True)
        anchors = acquire_reference_anchors(pool, [_Board()], timeout=0.1)
        self.assertEqual(anchors, [list(range(8))])
        self.assertEqual(
            pool.switch_calls,
            [RFSwitchState.SENSOR_RFSWITCH_REFERENCE, RFSwitchState.SENSOR_RFSWITCH_ANTENNA_L],
        )
        self.assertFalse(pool.emitting)
        self.assertIsNotNone(pool.removed)

    def test_timeout_also_restores_wifi_rf_switch(self):
        pool = _Pool(deliver=False)
        self.assertIsNone(acquire_reference_anchors(pool, [_Board()], timeout=0.0))
        self.assertEqual(pool.switch_calls[-1], RFSwitchState.SENSOR_RFSWITCH_ANTENNA_L)
        self.assertFalse(pool.emitting)
        self.assertIsNotNone(pool.removed)


if __name__ == "__main__":
    unittest.main()
