"""IQ calibration temporarily owns the controller radio, including from BLE."""

from contextlib import contextmanager
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from espargos.control_legacy import LegacyControllerResources
from espargos._iq_calibration import reference


class ReferenceModeTests(unittest.TestCase):
    def test_mode_restored_after_success_and_failure(self):
        for initial in ("ble", "wifi", "iq"):
            for fail in (False, True):
                with self.subTest(initial=initial, fail=fail):
                    current = initial
                    actions = []
                    control = Mock()
                    control.get_json.side_effect = lambda path: {"mode": initial} if path == "get_controller_mode" else {"enabled": False}

                    def select(path, config):
                        nonlocal current
                        self.assertEqual(path, "set_controller_mode")
                        current = config["mode"]
                        actions.append(current)

                    control.post_json.side_effect = select
                    iq = Mock()
                    iq.get_reference.return_value = {"mode": "off"}
                    pool = SimpleNamespace(master_board=SimpleNamespace(_resources=LegacyControllerResources(control), control=control, iq=iq), _reference_lock=Lock())

                    @contextmanager
                    def session(*args):
                        self.assertEqual(current, "iq")
                        try:
                            yield (2402e6, 10e6)
                        finally:
                            self.assertEqual(current, "iq")
                            actions.append("reference stopped")

                    with patch.object(reference, "_reference_session", session):
                        try:
                            with reference.reference_session(pool) as settings:
                                self.assertEqual(settings, (2402e6, 10e6))
                                if fail:
                                    raise TimeoutError("no reference packets")
                        except TimeoutError:
                            self.assertTrue(fail)
                    self.assertEqual(current, initial)
                    self.assertEqual(actions, ["reference stopped"] if initial == "iq" else ["iq", "reference stopped", initial])
                    self.assertFalse(pool._reference_lock.locked())

    def test_failed_mode_selection_releases_lock(self):
        control = Mock()
        control.get_json.return_value = {"mode": "ble"}
        control.post_json.side_effect = RuntimeError("controller busy")
        pool = SimpleNamespace(master_board=SimpleNamespace(_resources=LegacyControllerResources(control), control=control), _reference_lock=Lock())
        with patch.object(reference, "_reference_session") as session:
            with self.assertRaisesRegex(RuntimeError, "controller busy"):
                with reference.reference_session(pool):
                    self.fail("Calibration must not start")
            session.assert_not_called()
        self.assertFalse(pool._reference_lock.locked())


if __name__ == "__main__":
    unittest.main()
