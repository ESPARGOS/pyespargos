import copy
import socket
import unittest
from unittest.mock import Mock, patch, call

from espargos import Pool, Antenna
import espargos.pool as api


class FakeIQ:
    def __init__(self):
        self.config = {"modes": ["wifi"] * 8, "receivers": [{"antid": a, "rf_freq_hz": 2437000000} for a in range(8)], "adc_decimation": 1}
        self.writes = []

    def get_config(self):
        return copy.deepcopy(self.config)

    def set_config(self, payload):
        self.writes.append(copy.deepcopy(payload))
        for key, value in payload.items():
            if key == "receivers":
                for entry in value:
                    self.config[key][entry["antid"]].update(entry)
            else:
                self.config[key] = copy.deepcopy(value)


class FakeBoard(api.board_module.Board):
    def __init__(self, host):
        self.host = host
        self.api_version = (4, 0)
        self.iq = FakeIQ()
        self.control = Mock()
        self.control.get_json.side_effect = lambda path: {"sensors": [{"applied_mode": mode, "boot_id": a + 1} for a, mode in enumerate(self.iq.config["modes"])]} if path == "get_iq_tx" else {"mode": "wifi"}
        self.revision = Mock()
        self.revision.antenna_id_to_row_col.side_effect = lambda a: divmod(a, 4)
        self.start = Mock()
        self.close = Mock()
        self.wifi_rx = Mock()
        self.wifi_tx = Mock()
        self.iq.set_reference = Mock()
        self.wifi_rx.get_channel_overrides.return_value = {"override_active": False}
        self.wifi_rx.get_config.return_value = {"channel-primary": 6, "channel-secondary": 0}


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.boards = patch.object(api, "Board", side_effect=FakeBoard).start()
        self.rpc = patch.object(api, "_rpc", return_value={"sensors": [{"frequency_hz": 2437000000}] * 8}).start()
        self.wait = patch.object(api, "_wait").start()
        patch("espargos.configuration._rpc", self.rpc).start()
        patch("espargos.configuration._wait", self.wait).start()
        self.addCleanup(patch.stopall)

    def test_rx_sample_rate_selects_capture_path(self):
        with Pool("a") as pool:
            pool.configure(mode="iq_rx", sample_rate_hz=16000)
            iq = pool.boards[0].iq
            self.assertEqual(iq.config["trigger"], {"mode": "downsample", "sample_rate_hz": 16000})
            pool.configure(sample_rate_hz=32e3)
            self.assertEqual(iq.config["trigger"]["sample_rate_hz"], 32000)
            pool.configure(sample_rate_hz=20e6)
            self.assertEqual(iq.config["trigger"], {"mode": "interval"})
            self.assertEqual(iq.config["adc_decimation"], 4)
            iq.set_config({"trigger": {"mode": "signal", "threshold": 80}})
            pool.configure(sample_rate_hz=40e6)
            self.assertEqual(iq.config["trigger"], {"mode": "signal", "threshold": 80})

    def test_selections_are_lazy_and_non_owning(self):
        a = Pool(["a", "b"])
        view = a.select(boards="a", antennas=[1, 5])
        self.assertEqual(view.antennas, (Antenna("a", 1), Antenna("a", 5)))
        self.boards.assert_not_called()
        view.configure(mode="iq_rx", frequency_hz=2380000000)
        a.open()
        self.assertEqual(self.boards.call_count, 2)
        self.assertEqual(a.boards[0].iq.config["modes"], ["wifi", "iq_rx", "wifi", "wifi", "wifi", "iq_rx", "wifi", "wifi"])
        self.assertEqual(a.boards[1].iq.writes, [])
        self.assertEqual(a.boards[0].iq.config["receivers"][0]["rf_freq_hz"], 2437000000)
        a.close()
        a.close()
        for b in a.boards:
            b.close.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            view.configure(mode="wifi")

    def test_existing_boards_are_reused_and_owned_by_pool(self):
        first, second = FakeBoard("a"), FakeBoard("b")
        with Pool([first, second]) as pool:
            pool.open()
            self.assertEqual(pool.boards, [first, second])
            self.assertEqual(pool.select(boards=second).antennas[0], Antenna("b", 0))
            self.boards.assert_not_called()
            first.start.assert_called_once()
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_one_board_and_address_can_be_mixed_without_geometry(self):
        first = FakeBoard("a")
        with Pool([first, "b"]) as pool:
            self.assertIs(pool.boards[0], first)
            self.assertEqual(pool.hosts, ["a", "b"])
            self.assertIsNone(pool.indexing_matrix)
            self.boards.assert_called_once_with("b")
        with self.assertRaises(ValueError):
            Pool([first, "a"])

    def test_nested_selection_cannot_expand(self):
        a = Pool(["a", "b"])
        view = a.select(boards="a", antennas=[1, 3])
        for kwargs in [dict(boards=["missing"]), dict(antennas=[8]), dict(antennas=[Antenna("b", 1)]), dict(boards=["b"])]:
            with self.assertRaises(ValueError):
                view.select(**kwargs)
        self.boards.assert_not_called()

    def test_named_config_and_host_override(self):
        config = {"boards": {"front": {"host": "a"}, "rear": {"host": "b"}}, "array": [[f"{name}.{r}.{c}" for name in ("front", "rear") for c in range(4)] for r in range(2)]}
        a = Pool.from_config({"combined-array": config}, hosts=["x", "y"])
        self.assertEqual(a.names, ["front", "rear"])
        self.assertEqual(a.select(boards="rear").antennas[0], Antenna("y", 0))
        self.assertEqual(a.indexing_matrix.shape, (2, 8))
        self.boards.assert_not_called()

    def test_failed_lookup_names_the_board_and_closes_previous_connections(self):
        config = {"boards": {"front": {"host": "a"}, "rear": {"host": "b"}}, "array": [[f"{name}.{r}.{c}" for name in ("front", "rear") for c in range(4)] for r in range(2)]}
        pool = Pool.from_config(config, hosts=["first.local", "second.local"])
        first = FakeBoard("first.local")
        failure = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        self.boards.side_effect = [first, failure]
        with self.assertRaisesRegex(ConnectionError, r"rear \(second\.local\).*hostname lookup failed.*Name or service not known") as caught:
            pool.open()
        self.assertIs(caught.exception.__cause__, failure)
        first.close.assert_called_once()

    def test_reference_timeout_identifies_the_reference_controller(self):
        self.boards.side_effect = [FakeBoard("a"), TimeoutError("timed out")]
        with self.assertRaisesRegex(TimeoutError, "reference controller ref.local: timed out"):
            Pool("a", reference_controller="ref.local").open()

    def test_boardwide_request_is_rejected_before_any_write(self):
        a = Pool(["a", "b"])
        a.open()
        for b in a.boards:
            b.iq.config["modes"] = ["iq_rx"] * 8
        selection = a.select(antennas=[Antenna("a", i) for i in range(8)] + [Antenna("b", 1)])
        with self.assertRaisesRegex(ValueError, "board-wide"):
            selection.configure(sample_rate_hz=40000000)
        self.assertFalse(any(b.iq.writes for b in a.boards))
        self.rpc.assert_not_called()

    def test_wifi_channels_only_change_selected_antennas(self):
        a = Pool("a")
        a.select(antennas=[2, 5]).configure(frequency_hz=2412000000)
        args = a.boards[0].wifi_rx.set_channel_overrides.call_args.args[0]
        self.assertEqual(args["channel-primary"], [6, 6, 1, 6, 6, 1, 6, 6])
        self.assertTrue(args["override_active"])

    def test_secondary_below_uses_firmware_channel_encoding(self):
        a = Pool("a")
        a.select(antennas=[2]).configure(channel=6, secondary_channel=-1)
        sent = a.boards[0].wifi_rx.set_channel_overrides.call_args.args[0]
        self.assertEqual(sent["channel-secondary"], [0, 0, 2, 0, 0, 0, 0, 0])

    def test_entering_tx_does_not_start_previous_waveform(self):
        a = Pool("a")
        a.select(antennas=[2]).configure(mode="iq_tx", frequency_hz=2380000000)
        payload = self.rpc.call_args.args[2]
        self.assertEqual(payload, {"transmitters": [{"antid": 2, "frequency_hz": 2380000000, "amplitude": 0}]})
        self.assertEqual(a.boards[0].iq.config["modes"][2], "iq_tx")

    def test_receive_pools_share_connections_and_are_closed(self):
        iq, csi = Mock(), Mock()
        with patch.dict(Pool._receiver_factories, iq=(iq, "iq_rx"), wifi=(csi, "wifi")):
            with Pool("a") as a:
                self.assertIs(a.iq, a.iq)
                self.assertIs(a.wifi, a.wifi)
                iq.assert_called_once_with(a)
                csi.assert_called_once_with(a)
                self.assertEqual(self.boards.call_count, 1)
            iq.return_value._close.assert_called_once()
            csi.return_value._close.assert_called_once()

    def test_reference_scope_is_explicit_not_inferred_from_geometry(self):
        from espargos import Calibration

        config = {"boards": {"one": {"host": "a"}, "two": {"host": "b"}}, "array": [[f"{name}.{r}.{c}" for name in ("one", "two") for c in range(4)] for r in range(2)]}
        with patch("espargos.csi_scoped_calibration.calibrate_full_array", return_value=Calibration()) as calibrate:
            with Pool.from_config(config) as owner:
                owner.calibrate()
                self.assertTrue(calibrate.call_args.kwargs["per_board"])
                owner.calibrate(per_board=False)
                self.assertFalse(calibrate.call_args.kwargs["per_board"])

    def test_reconnect_preserves_selected_transport(self):
        with Pool("a") as owner:
            owner._transports = ["websocket"]
            owner.boards[0].stop = Mock()
            owner.boards[0].start.reset_mock()
            owner.reconnect()
            owner.boards[0].stop.assert_called_once()
            owner.boards[0].start.assert_called_once_with(transports=["websocket"])

    def test_closed_tx_controls_do_not_interfere_with_configuration_or_shutdown(self):
        owner = Pool("a").open()
        tx = owner._tx = Mock(closed=True)
        owner.configure(mode="iq_rx")
        tx.read.assert_not_called()
        self.rpc.reset_mock()
        owner.close()
        self.rpc.assert_not_called()
        tx.close.assert_not_called()

    def test_receive_only_shutdown_does_not_touch_transmitters_or_reference(self):
        owner = Pool(["a", "b"]).open()
        self.rpc.reset_mock()
        owner.close()
        self.rpc.assert_not_called()
        for board in owner.boards:
            board.wifi_tx.set_config.assert_not_called()
            board.iq.set_reference.assert_not_called()
            board.close.assert_called_once()

    def test_shutdown_mutes_only_boards_started_by_selection(self):
        state = {"muted": True, "sensors": [{"applied_mode": "iq_tx", "samples": 1024} for _ in range(8)]}
        self.rpc.side_effect = lambda b, command, *args: ({"authorized": True} if command == "get_transmit_authorization" else state)
        owner = Pool(["a", "b"])
        owner.select(boards="b").start_transmission()
        self.rpc.reset_mock()
        owner.close()
        self.rpc.assert_called_once_with(owner.boards[1], "set_iq_tx", {"muted": True})

    def test_shutdown_closes_owned_iq_transmitter(self):
        owner = Pool("a").open()
        tx = owner._tx = Mock(closed=False)
        owner.close()
        tx.close.assert_called_once()

    def test_shutdown_stops_only_explicitly_started_reference(self):
        from espargos.iq_receiver import IQReceiver

        owner = Pool(["a", "b"]).open()
        receiver = Mock(_owner=owner, master_board=owner.boards[1])
        IQReceiver.set_reference_tone(receiver, 2437000000)
        owner.boards[1].iq.set_reference.reset_mock()
        owner.close()
        owner.boards[0].iq.set_reference.assert_not_called()
        owner.boards[1].iq.set_reference.assert_called_once_with()

    def test_shutdown_stops_configured_wifi_transmitters(self):
        owner = Pool(["a", "b"]).open()
        from espargos.wifi_transceiver import WiFiTransceiver

        wifi = Mock(_owner=owner, boards=owner.boards)
        WiFiTransceiver.set_tx_config(wifi, {"active_by_antid": [True] * 8})
        for board in owner.boards:
            board.wifi_tx.set_config.reset_mock()
        owner.close()
        for board in owner.boards:
            board.wifi_tx.set_config.assert_called_once_with({"active_by_antid": [False] * 8})

    def test_failed_shutdown_still_closes_every_connection(self):
        a = Pool(["a", "b"])
        a.open()
        a.boards[0].close.side_effect = RuntimeError("broken")
        with self.assertRaises(ExceptionGroup):
            a.close()
        a.boards[1].close.assert_called_once()

    def test_iq_frequency_mismatch_fails_before_calibration(self):
        a = Pool(["a", "b"])
        a.open()
        for b in a.boards:
            b.iq.config["modes"] = ["iq_rx"] * 8
        a.boards[1].iq.config["receivers"][2]["rf_freq_hz"] = 2412000000
        with self.assertRaisesRegex(ValueError, "one frequency"):
            a.calibrate()
        self.assertNotIn("iq", a._receivers)
        self.assertFalse(any(b.iq.writes for b in a.boards))

    def test_subset_start_keeps_other_transmitters_silent(self):
        state = {"muted": True, "sensors": [{"applied_mode": "iq_tx", "samples": 1024} for _ in range(8)]}
        self.rpc.side_effect = lambda b, command, *args: ({"authorized": True} if command == "get_transmit_authorization" else state)
        a = Pool("a")
        a.select(antennas=[1, 5]).start_transmission(amplitude=0.2)
        writes = [c.args[2] for c in self.rpc.call_args_list if c.args[1] == "set_iq_tx"]
        self.assertEqual(writes, [{"muted": False, "transmitters": [{"antid": i, "amplitude": 0.2 if i in (1, 5) else 0} for i in range(8)]}])

    def test_start_preflights_all_boards_before_unmuting(self):
        def rpc(board, command, *args):
            if command == "get_transmit_authorization":
                return {"authorized": board.host == "a"}
            return {"muted": True, "sensors": [{"applied_mode": "iq_tx", "samples": 1024} for _ in range(8)]}

        self.rpc.side_effect = rpc
        with self.assertRaises(PermissionError):
            Pool(["a", "b"]).start_transmission()
        self.assertFalse(any(c.args[1] == "set_iq_tx" for c in self.rpc.call_args_list))

    def test_subset_stop_does_not_mute_other_antennas(self):
        Pool("a").select(antennas=[3]).stop_transmission()
        self.assertEqual(self.rpc.call_args.args[2], {"transmitters": [{"antid": 3, "amplitude": 0}]})


if __name__ == "__main__":
    unittest.main()


class OwnershipTests(unittest.TestCase):
    def test_receivers_borrow_owner_and_have_no_lifecycle(self):
        from espargos import IQReceiver, WiFiTransceiver, Calibration

        owner = Mock(boards=[], calibrations=Calibration())
        iq, csi = IQReceiver(owner), WiFiTransceiver(owner)
        try:
            for receiver in (iq, csi):
                self.assertIs(receiver._owner, owner)
                for name in ("calibrations", "start_processing", "stop_processing", "run", "close"):
                    self.assertFalse(hasattr(receiver, name), name)
        finally:
            iq._close()
            csi._close()

    def test_mixed_csi_getters_never_write(self):
        from espargos import WiFiTransceiver

        csi = WiFiTransceiver(Mock(boards=[]))
        boards = [Mock(), Mock()]
        csi.boards = boards
        getters = [
            ("get_rf_switch", "get_rf_switch"),
            ("get_mac_filter", "get_mac_filter"),
            ("get_gain_settings", "get_gain_settings"),
            ("get_cfo_correction", "get_cfo_correction"),
            ("get_csi_acquisition_config", "get_csi_acquisition_config"),
            ("get_wifi_channel_overrides", "get_channel_overrides"),
            ("get_wifi_config", "get_config"),
        ]
        for public, local in getters:
            for i, board in enumerate(boards):
                board.reset_mock()
                getattr(board.wifi_rx, local).return_value = {"value": i}
            with self.subTest(getter=public), self.assertRaisesRegex(ValueError, "differ"):
                getattr(csi, public)()
            for board in boards:
                self.assertEqual(board.wifi_rx.method_calls, [getattr(call, local)()])
        csi._close()

    def test_board_local_settings_do_not_require_equality(self):
        from espargos import WiFiTransceiver

        self.assertEqual(WiFiTransceiver._common_setting([{"channel": 6, "local": 1}, {"channel": 6, "local": 2}], "wifi", {"local"}), {"channel": 6, "local": 1})


class ProcessingTests(unittest.TestCase):
    setUp = PoolTests.setUp

    def test_invalid_calibration_duration_has_no_hardware_side_effects(self):
        owner = Pool("a")
        for duration in (0, -1, float("nan")):
            with self.assertRaisesRegex(ValueError, "duration"):
                owner.calibrate(duration=duration)
        self.boards.assert_not_called()

    def test_one_worker_processes_receivers_created_later(self):
        import threading

        csi_seen, iq_seen = threading.Event(), threading.Event()
        iq, csi = Mock(), Mock()
        with patch.dict(Pool._receiver_factories, iq=(iq, "iq_rx"), wifi=(csi, "wifi")):
            csi.return_value._run.side_effect = lambda **k: (csi_seen.set(), 0)[1]
            iq.return_value._run.side_effect = lambda **k: (iq_seen.set(), 0)[1]
            with Pool("a") as owner:
                owner.wifi
                owner.start_processing()
                worker = owner._processing_thread
                self.assertTrue(csi_seen.wait(1))
                owner.iq
                owner.start_processing()
                self.assertIs(owner._processing_thread, worker)
                self.assertTrue(iq_seen.wait(1))
                with self.assertRaisesRegex(RuntimeError, "background"):
                    owner.run()
                owner.stop_processing()
                self.assertFalse(worker.is_alive())
                self.assertFalse(owner.processing)
                self.assertEqual(owner.run(timeout=0), 0)

    def test_callback_can_stop_its_own_worker(self):
        import threading

        stopped = threading.Event()
        with Pool("a") as owner:
            receiver = owner._receivers["wifi"] = Mock()

            def callback(**kwargs):
                owner.stop_processing()
                stopped.set()
                return 0

            receiver._run.side_effect = callback
            owner.start_processing()
            self.assertTrue(stopped.wait(1))
            owner.stop_processing()
            self.assertFalse(owner._processing_thread.is_alive())

    def test_selections_do_not_reserve_roles_and_masks_follow_changes(self):
        import numpy as np

        with Pool("a") as owner:
            owner._receivers["wifi"] = Mock()
            owner._receivers["iq"] = Mock()
            a, b = owner.select(antennas=[0, 1]), owner.select(antennas=[1, 2])
            a.configure(mode="wifi")
            b.configure(mode="iq_tx")
            self.assertEqual(owner.boards[0].iq.config["modes"][:3], ["wifi", "iq_tx", "iq_tx"])
            self.assertEqual(owner._receivers["wifi"]._expected.reshape(-1).tolist(), [True, False, False, True, True, True, True, True])
            self.assertFalse(np.any(owner._receivers["iq"]._expected))

    def test_mixed_roles_dispatch_wifi_and_tx_calibration(self):
        from espargos import Calibration

        with Pool(["a", "b"]) as owner:
            owner.boards[1].iq.config["modes"][1] = "iq_tx"
            owner._receivers["iq"] = Mock(boards=owner.boards)
            with patch("espargos._iq_calibration.orchestration.calibrate", return_value=Calibration()) as iq, patch.object(owner.wifi, "calibrate_channels", return_value=Calibration()) as wifi:
                owner.select(antennas=[Antenna("a", 0), Antenna("b", 1)]).calibrate(tx_method="monitor")
                self.assertEqual(iq.call_args.kwargs["scope"].antennas, {Antenna("b", 1)})
                self.assertEqual(wifi.call_args.kwargs["antennas"], {Antenna("a", 0)})

    def test_mixed_roles_on_same_board_can_calibrate(self):
        from espargos import Calibration

        with Pool("a") as owner:
            owner.boards[0].iq.config["modes"][0] = "iq_rx"
            iq = owner._receivers["iq"] = Mock(boards=owner.boards)
            with patch.object(owner.wifi, "calibrate_channels", return_value=Calibration()) as wifi, patch("espargos._iq_calibration.orchestration.snapshot", return_value=Calibration()):
                owner.calibrate()
            self.assertEqual(iq._sync.call_args.kwargs["scope"].antennas, {Antenna("a", 0)})
            self.assertEqual(wifi.call_args.kwargs["antennas"], {Antenna("a", a) for a in range(1, 8)})

    def test_mixed_rx_calibration_finishes_iq_before_wifi(self):
        from espargos import Calibration

        with Pool(["a", "b"]) as owner:
            owner.boards[1].iq.config["modes"] = ["iq_rx"] * 8
            iq = owner._receivers["iq"] = Mock(boards=owner.boards)

            def wifi(**kwargs):
                self.assertEqual(iq._sync.call_args.kwargs["scope"].active, [1])
                self.assertEqual(kwargs["antennas"], {Antenna("a", a) for a in range(8)})
                return Calibration()

            with patch.object(owner.wifi, "calibrate_channels", side_effect=wifi) as measure, patch("espargos._iq_calibration.orchestration.snapshot", return_value=Calibration()):
                owner.calibrate()
            measure.assert_called_once()

    def test_default_callbacks_only_wait_for_their_roles(self):
        import numpy as np
        from types import SimpleNamespace
        from espargos import IQReceiver, WiFiTransceiver

        for cls in (IQReceiver, WiFiTransceiver):
            receiver = cls(SimpleNamespace(boards=[]))
            receiver._expected = np.array([[[True, False, True, False], [False] * 4]])
            cluster = SimpleNamespace(completion=receiver._expected.copy())
            self.assertTrue(receiver._complete(cluster))
            cluster.completion[0, 0, 2] = False
            self.assertFalse(receiver._complete(cluster))
            receiver._expected[:] = False
            self.assertFalse(receiver._complete(cluster))
            receiver._close()
