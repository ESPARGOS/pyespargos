import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from demos.common.csi_application import SingleCSIFormatMixin
from demos.common.polarization import PolarizationMixin
from espargos.combined_array import AntennaOrientation


class Demo(SingleCSIFormatMixin, PolarizationMixin):
    pass


class ChannelTransitionTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"channel": 6, "secondary_channel": 1}
        self.demo = Demo()
        self.demo.pooldrawer = SimpleNamespace(configManager=lambda: SimpleNamespace(get=self.settings.get))
        self.demo.csiconfig = SimpleNamespace(get=lambda key: "auto")
        self.demo.backlog = Mock(has_data=True)
        self.demo.backlog.count_valid_datapoints.side_effect = lambda fmt, **kw: 100 if fmt == "ht40" else 1
        self.demo.backlog.get_multiple.side_effect = lambda keys: [np.ones((1, 1, 1, 1, 53), complex)]
        self.demo.antenna_orientations = np.array([[AntennaOrientation.N]], dtype=object)

    def test_auto_discards_old_ht40_then_accepts_new_samples(self):
        self.settings["secondary_channel"] = 0
        self.assertIsNone(self.demo.get_backlog_csi())
        self.demo.backlog.clear.assert_called_once()
        self.demo.backlog.get_multiple.assert_not_called()
        self.demo.backlog.count_valid_datapoints.side_effect = lambda fmt, **kw: 1 if fmt == "lltf" else 0
        fmt, csi = self.demo.get_backlog_csi(return_format=True, remove_global_sto=False)
        corrected = self.demo.correct_polarization(np.stack([csi, csi], axis=-1), fmt)
        self.assertEqual(corrected.shape, (*csi.shape, 2))

    def test_ht40_is_accepted_after_secondary_reenabled(self):
        self.settings["secondary_channel"] = 1
        self.demo.backlog.get_multiple.return_value = [np.ones((1, 1, 1, 1, 117), complex)]
        self.demo.backlog.get_multiple.side_effect = None
        result = self.demo.get_backlog_csi(return_format=True, remove_global_sto=False)
        self.assertEqual(result[0], "ht40")
        self.demo.backlog.clear.assert_not_called()

    def test_explicit_ht40_waits_until_secondary_enabled(self):
        self.settings["secondary_channel"] = 0
        self.demo.csiconfig = SimpleNamespace(get=lambda key: "ht40")
        self.assertIsNone(self.demo.get_backlog_csi())
        self.demo.backlog.get_multiple.assert_not_called()


if __name__ == "__main__":
    unittest.main()
