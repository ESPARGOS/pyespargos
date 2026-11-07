"""GUI reference operations must not overlap queued manual tone writes."""

import importlib.util
import threading
import time
from types import SimpleNamespace
import unittest


@unittest.skipUnless(importlib.util.find_spec("PyQt6"), "requires optional demo dependencies")
class ReferenceControllerTests(unittest.TestCase):
    def test_sync_waits_for_tone_and_discards_queued_edits(self):
        from PyQt6 import QtCore
        from demos.common.iq_application import IQController

        app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
        entered, release, synced = (threading.Event() for _ in range(3))
        calls = []

        def tone(*args, **kwargs):
            calls.append("tone-start")
            entered.set()
            release.wait(3)
            calls.append("tone-end")

        def sync(**kwargs):
            calls.append("sync")
            synced.set()

        controller = IQController(SimpleNamespace(set_reference_tone=tone, sync=sync, add_callback=lambda cb: None, add_accumulation_callback=lambda cb: None))
        try:
            controller.reference_apply("cw", 2437, 7.5)
            self.assertTrue(entered.wait(2))
            controller.reference_apply("cw", 2440, 7.5)
            controller.sync_async()
            controller.sync_async()
            self.assertTrue(controller.referenceBusy)
            self.assertFalse(synced.wait(0.1))
            release.set()
            self.assertTrue(synced.wait(2))
            deadline = time.monotonic() + 2
            while (controller.referenceBusy or controller._tone_busy) and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            self.assertEqual(calls, ["tone-start", "tone-end", "sync"])
            self.assertFalse(controller.referenceBusy)
            self.assertFalse(controller._tone_busy)
        finally:
            release.set()

    def test_sync_state_tracks_configuration_epochs_and_failure(self):
        from demos.common.iq_application import IQController

        callbacks = []
        pool = SimpleNamespace(
            add_callback=callbacks.append,
            add_accumulation_callback=callbacks.append,
            calibration=object(),
            calibration_center_frequency=2437e6,
            calibration_sample_rate=80e6,
            calibration_applies_to=lambda capture: capture == "valid",
            apply_config=lambda config: None,
        )
        controller = IQController(pool)
        self.assertTrue(controller.syncNeeded)
        controller._begin_reference()
        controller._end_reference(True)
        self.assertFalse(controller.syncNeeded)
        controller.apply_config_json('{"rx_gain": 40}')
        self.assertFalse(controller.syncNeeded)
        controller.apply_config_json('{"adc_decimation": 4}')
        self.assertTrue(controller.syncNeeded)
        controller._end_reference(True)
        controller.apply_config_json('{"receivers": [{"rf_freq_hz": 2438000000}]}')
        self.assertTrue(controller.syncNeeded)
        for callback in callbacks:
            controller._end_reference(True)
            callback("valid")
            self.assertFalse(controller.syncNeeded)
            callback("new epoch")
            self.assertTrue(controller.syncNeeded)
        controller._begin_reference()
        controller._end_reference(False)
        self.assertTrue(controller.syncNeeded)
        self.assertFalse(controller.referenceBusy)

    def test_iq_demo_selects_roles_before_creating_receiver(self):
        from demos.common.iq_application import ESPARGOSIQApplication

        calls = []

        class Owner:
            def configure(self, **config):
                calls.append(config)

            @property
            def iq(self):
                self_test.assertEqual(calls, [{"mode": "iq_rx"}])
                return receiver

        self_test = self
        receiver = object()
        self.assertIs(ESPARGOSIQApplication._create_receiver(None, Owner()), receiver)

    def test_manual_noise_uses_canonical_reference_api(self):
        from demos.common.iq_application import IQController

        calls = []
        pool = SimpleNamespace(
            add_callback=lambda cb: None,
            add_accumulation_callback=lambda cb: None,
            master_board=SimpleNamespace(iq=SimpleNamespace(set_reference=lambda *args: calls.append(args))),
            set_reference_tone=lambda **kwargs: calls.append(kwargs),
        )
        controller = IQController(pool)
        controller._apply_tone_request("iq", 2437, 7.5)
        self.assertEqual(calls[-1], ("iq", 2437000000, 30))
        controller._apply_tone_request("iq", 2640, 7.5)
        self.assertEqual(calls[-1], ("iq", 2640000000, 30))
        with self.assertRaises(ValueError):
            controller._apply_tone_request("iq", 2641, 7.5)
        controller._apply_tone_request("off", 2437, 7.5)
        self.assertEqual(calls[-1], {"enable": False})
