#!/usr/bin/env python

"""Configure Wi-Fi reception and combine per-sensor CSI into packet clusters.

Every sensor reports CSI independently, while applications generally want one
array-shaped measurement for each received Wi-Fi packet. :class:`CSIPool`
associates reports using their MAC addresses and sequence control field, stores
them in :class:`.CSICluster` objects, and emits those clusters when a callback's
completion requirement is met. It also provides the pool-wide Wi-Fi, radar, and
calibration API.
"""

from weakref import WeakKeyDictionary
from typing import Callable
import numpy as np
import threading
import time

from . import calibration
from . import board
from . import util
from . import csi_cluster
from . import csi_packet
from . import radar_packet
from . import sensor
from . import radar
from . import wifi
from .pool import Pool
from .sensor_cluster import SensorCluster


class CalibrationError(RuntimeError):
    """Raised when ESPARGOS calibration cannot collect usable calibration CSI."""


class _CSICallback(object):
    def __init__(
        self,
        cb: Callable[[csi_cluster.CSICluster], None],
        cb_predicate: Callable[[csi_cluster.CSICluster], bool] = None,
    ):
        # By default, provide csi if CSI is available from all antennas
        self.cb_predicate = cb_predicate
        self.cb = cb

        # Track fired state per CSICluster object
        self.fired = WeakKeyDictionary()

    def try_call(self, cluster_obj: csi_cluster.CSICluster):
        # Check if callback has already been fired for this CSICluster object
        if self.fired.get(cluster_obj, False):
            return True

        # Check if callback needs to be called: Use predicate function if defined, otherwise call if all antennas have CSI
        callback_required = False
        if self.cb_predicate is not None:
            callback_required = self.cb_predicate(cluster_obj)
        else:
            callback_required = cluster_obj.get_completion_all()

        if callback_required:
            self.cb(cluster_obj)

            # Mark as fired for this CSICluster object
            self.fired[cluster_obj] = True
            return True

        return False


# WiFi configuration fields that legitimately differ between boards. Pool-wide
# reads and writes must leave these board-local calibration settings untouched.
WIFICONF_PER_BOARD_KEYS = {
    "calib-source",
    "calib-mode",
    "calib-txpower",
    "calib-interval",
}

_CACHE_OTA = "ota"
_CACHE_CALIBRATION = "calibration"


class CSIPool(Pool):
    """Manage Wi-Fi features and assemble CSI for each received Wi-Fi packet."""

    def __init__(self, boards: list[board.Board], ota_cache_timeout=5, refgen_boards=None):
        """
        Constructor for the CSIPool class.

        :param boards: A list of ESPARGOS boards that belong to the pool
        :param ota_cache_timeout: Optional. The timeout in seconds after which over-the-air CSI data is considered stale and discarded
                                  if the cluster is not complete
        :param refgen_boards: Optional. In some multi-board setups, the calibration signal is provided by (a) separate ESPARGOS device(s)
                              that is / are not part of the pool (only controller is used to generate packets, sensors not used).
                              If provided, sends calibration command to these boards, which will then generate the calibration signal
                              during calibration phase.
        """
        super().__init__(boards)
        self.refgen_boards = refgen_boards if refgen_boards is not None else []

        self.ota_cache_timeout = ota_cache_timeout
        self.emit_calibration_csi = False

        for board_num, board_obj in enumerate(self.boards):
            wifi_rx = board_obj.wifi_rx
            wifi_tx = board_obj.wifi_tx
            self._subscribe_sensor_messages(
                board_num,
                wifi_rx,
                wifi_rx.subscribe_csi,
            )
            self._subscribe_sensor_messages(
                board_num,
                wifi_tx,
                wifi_tx.subscribe_reports,
            )

        self.callbacks: list[_CSICallback] = []
        self._callbacks_lock = threading.Lock()
        self.stored_calibration: calibration.CSICalibration = None

    def set_rfswitch(self, state: sensor.RFSwitchState):
        """
        Set RF switch state for all boards in the pool.

        :param state: The RF switch state to set, must be one of :class:`sensor.RFSwitchState`
        """
        for board in self.boards + self.refgen_boards:
            board.wifi_rx.set_rf_switch(state)

    def get_rfswitch(self) -> sensor.RFSwitchState:
        """
        Get RF switch state from the first board in the pool.

        :return: The RF switch state of the first board in the pool
        """
        if not self.boards:
            raise ValueError("No boards in pool to get RF switch state from")

        states = [b.wifi_rx.get_rf_switch() for b in self.boards]
        return self._reconcile_across_boards(
            states,
            "RF switch state",
            lambda value: [board.wifi_rx.set_rf_switch(value) for board in self.boards],
        )

    def set_mac_filter(self, mac_filter: dict):
        """
        Set the MAC address filter for all boards in the pool. Will only accept packets from the specified MAC address.

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRx.set_mac_filter` for each board.
        """
        for board in self.boards:
            board.wifi_rx.set_mac_filter(mac_filter)

    def clear_mac_filter(self):
        """
        Clear the MAC address filter for all boards in the pool.
        """
        for board in self.boards:
            board.wifi_rx.clear_mac_filter()

    def get_mac_filter(self) -> dict:
        """
        Return MAC filter configuration, reconciling boards when needed.

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRx.get_mac_filter` for each board.
        """
        filters = [b.wifi_rx.get_mac_filter() for b in self.boards]
        return self._reconcile_across_boards(
            filters,
            "MAC filter",
            lambda value: [board.wifi_rx.set_mac_filter(value) for board in self.boards],
        )

    def get_csi_acquire_config(self) -> dict:
        """
        Return CSI acquire config, reconciling boards when needed.
        """
        cfgs = [b.wifi_rx.get_csi_acquisition_config() for b in self.boards]
        return self._reconcile_across_boards(
            cfgs,
            "CSI acquire config",
            lambda value: [board.wifi_rx.set_csi_acquisition_config(value) for board in self.boards],
        )

    def set_csi_acquire_config(self, config: dict):
        """
        Set CSI acquisition configuration on all boards in this pool and sanity-check that all boards
        end up with the same config.

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRx.set_csi_acquisition_config` for each board.
        For the expected JSON/dict format, refer to that method's documentation.

        :param config: CSI acquisition configuration dict to apply to all boards.
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        for b in self.boards:
            b.wifi_rx.set_csi_acquisition_config(config)
        _ = self.get_csi_acquire_config()

    def get_cfo_correction(self) -> dict:
        """
        Return CFO correction config, reconciling boards when needed.
        """
        configs = [b.wifi_rx.get_cfo_correction() for b in self.boards]
        return self._reconcile_across_boards(
            configs,
            "CFO correction",
            lambda value: [board.wifi_rx.set_cfo_correction(value["auto"], value.get("value", 0)) for board in self.boards],
        )

    def set_cfo_correction(self, auto: bool, value: int = 0):
        """
        Configure CFO correction on all boards in this pool.
        """
        for b in self.boards:
            b.wifi_rx.set_cfo_correction(auto, value)
        _ = self.get_cfo_correction()

    def get_gain_settings(self) -> dict:
        """
        Return gain settings, resetting mismatched boards to automatic gain.
        """
        settings = [b.wifi_rx.get_gain_settings() for b in self.boards]
        return self._reconcile_across_boards(
            settings,
            "Gain settings",
            lambda value: self.set_gain_settings(value),
            reset_value={
                "rx_gain_enable": False,
                "fft_scale_enable": False,
                "rx_gain_value": 32,
                "fft_scale_value": 0,
            },
        )

    def set_gain_settings(self, settings: dict):
        """
        Set gain settings on all boards in this pool.

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRx.set_gain_settings` for each board.
        Values may be scalars, board-local ``(row, column)`` arrays applied to every
        board, or pool-wide ``(board, row, column)`` arrays.

        :param settings: Gain settings dict to apply to all boards.
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        per_board_settings = [dict() for _ in self.boards]
        for key, value in settings.items():
            array = np.asarray(value)
            if array.shape == self.get_shape():
                for board_index in range(len(self.boards)):
                    per_board_settings[board_index][key] = array[board_index]
            else:
                for board_settings in per_board_settings:
                    board_settings[key] = value

        for board_obj, board_settings in zip(self.boards, per_board_settings):
            board_obj.wifi_rx.set_gain_settings(board_settings)

    def get_wifi_channel_overrides(self) -> dict:
        """
        Return per-sensor WiFi channel overrides, reconciling boards when needed.
        """
        settings = [b.wifi_rx.get_channel_overrides() for b in self.boards]
        return self._reconcile_across_boards(
            settings,
            "WiFi channel overrides",
            lambda value: [board.wifi_rx.set_channel_overrides(value) for board in self.boards],
        )

    def set_wifi_channel_overrides(self, settings: dict):
        """
        Set per-sensor WiFi channel overrides on all boards in this pool and sanity-check that all boards end up with the same settings.

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRx.set_channel_overrides` for each board.
        For the expected JSON/dict format, refer to that method's documentation.

        :param settings: Per-sensor WiFi channel override settings dict to apply to all boards.
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        for b in self.boards:
            b.wifi_rx.set_channel_overrides(settings)
        _ = self.get_wifi_channel_overrides()

    def get_radar_configs(self) -> list[dict]:
        """
        Return radar TX configuration for all boards in the pool.
        """
        return [b.wifi_tx.get_config() for b in self.boards]

    def get_radar_config(self) -> dict:
        """
        Return radar TX configuration; sanity-check all boards report the same value.
        """
        configs = self.get_radar_configs()
        self._assert_same_across_boards(configs, "Radar config")
        return configs[0]

    def set_radar_config(self, config: dict | radar.RadarPoolConfig):
        """
        Set radar TX configuration on the boards in this pool.

        ``config`` may either be a single controller config dict applied to every board,
        or a :class:`pyespargos.espargos.radar.RadarPoolConfig` containing one config per board.
        """
        if isinstance(config, radar.RadarPoolConfig):
            if len(config.board_configs) != len(self.boards):
                raise ValueError(f"RadarPoolConfig contains {len(config.board_configs)} board configs, expected {len(self.boards)}")
            for board_obj, board_config in zip(self.boards, config.board_configs):
                board_obj.wifi_tx.set_config(board_config)
            return

        for b in self.boards:
            b.wifi_tx.set_config(config)

    def get_wificonf(self) -> dict:
        """
        Return WiFi config, reconciling pool-wide settings when boards disagree.

        Board-local calibration fields are excluded from both comparison and
        reconciliation, so each board retains its own values.
        """
        wificonfs = [b.wifi_rx.get_config() for b in self.boards]
        return self._reconcile_across_boards(
            wificonfs,
            "WiFi config",
            lambda value: [board.wifi_rx.set_config(value) for board in self.boards],
            ignore_keys=WIFICONF_PER_BOARD_KEYS,
        )

    def set_wificonf(self, wificonf: dict):
        """
        Set WiFi config on all boards and sanity-check resulting configs match across boards.

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRx.set_config` for each board.
        For the expected JSON/dict format, refer to that method's documentation.

        Fields listed in :data:`WIFICONF_PER_BOARD_KEYS` are ignored and not
        propagated because they may legitimately differ between boards. Set
        those directly through :meth:`pyespargos.board_wifi_rx.WiFiRx.set_config`.

        :param wificonf: WiFi configuration dict to apply to all boards.
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        wificonf = {key: value for key, value in wificonf.items() if key not in WIFICONF_PER_BOARD_KEYS}
        for b in self.boards:
            b.wifi_rx.set_config(wificonf)
        _ = self.get_wificonf()

    def reboot(self):
        """
        Trigger a reboot on all boards in the pool.
        """
        super().reboot()
        for board_obj in self.refgen_boards:
            board_obj.reboot()

    def add_csi_callback(
        self,
        cb: Callable[[csi_cluster.CSICluster], None],
        cb_predicate: Callable[[csi_cluster.CSICluster], bool] = None,
    ):
        """
        Register callback function that is invoked whenever a new CSI cluster is completed.

        :param cb: The function to call, gets an instance of :class:`.csi_cluster.CSICluster`
        :param cb_predicate: A function with signature :code:`(csi_cluster)` that defines the conditions under which
            clustered CSI is regarded as completed and thus provided to the callback.
            If :code:`cb_predicate` returns true, clustered CSI is regarded as completed.
            If no predicate is provided, the default behavior is to trigger the callback when CSI has been received
            from all sensors on all boards. By default, callbacks receive over-the-air/radar CSI only. Enable
            :meth:`set_emit_calibration_csi` to also emit calibration CSI (from internal reference generators)
            through this same callback path.
        :return: A callback handle that can be passed to :meth:`remove_csi_callback`
        """
        callback = _CSICallback(cb, cb_predicate)
        with self._callbacks_lock:
            self.callbacks.append(callback)
        return callback

    def remove_csi_callback(self, callback: _CSICallback) -> bool:
        """
        Remove a CSI callback previously returned by :meth:`add_csi_callback`.

        :param callback: Callback handle returned by :meth:`add_csi_callback`
        :return: True if the callback was registered and removed, False otherwise
        """
        with self._callbacks_lock:
            try:
                self.callbacks.remove(callback)
                return True
            except ValueError:
                return False

    def replace_csi_callback(
        self,
        callback: _CSICallback,
        cb: Callable[[csi_cluster.CSICluster], None],
        cb_predicate: Callable[[csi_cluster.CSICluster], bool] = None,
    ) -> _CSICallback:
        """Atomically replace a registered CSI callback.

        :param callback: Existing handle returned by :meth:`add_csi_callback`
        :param cb: Replacement callback function
        :param cb_predicate: Replacement completion predicate
        :return: New callback handle
        :raises ValueError: If ``callback`` is no longer registered
        """

        replacement = _CSICallback(cb, cb_predicate)
        with self._callbacks_lock:
            try:
                index = self.callbacks.index(callback)
            except ValueError:
                raise ValueError("CSI callback is not registered") from None
            self.callbacks[index] = replacement
        return replacement

    def set_emit_calibration_csi(self, enabled: bool):
        """
        Control whether calibration CSI clusters are emitted through normal CSI callbacks.

        Calibration clusters remain marked as calibration packets via
        :meth:`espargos.csi_cluster.CSICluster.is_calib`.
        """
        self.emit_calibration_csi = bool(enabled)

    def get_emit_calibration_csi(self) -> bool:
        """
        Return whether calibration CSI clusters are emitted through normal CSI callbacks.
        """
        return self.emit_calibration_csi

    def _try_callbacks(self, cluster_obj: csi_cluster.CSICluster) -> bool:
        all_callbacks_fired = True
        with self._callbacks_lock:
            callbacks = tuple(self.callbacks)
        for cb in callbacks:
            callback_fired = cb.try_call(cluster_obj)
            all_callbacks_fired = callback_fired and all_callbacks_fired
        return all_callbacks_fired

    def _clusters_to_calibration(self, board_num=None):
        """
        Convert collected calibration clusters to phase calibration values.

        :param board_num: If provided, only process calibration clusters for the specified board number
        """
        clusters = self._get_cluster_cache_snapshot(_CACHE_CALIBRATION)

        # Collection of complete clusters (= reference CSI data from all antennas available): L-LTF, HT20-LTF, and HT40-LTF
        complete_clusters_lltf = []
        complete_cluster_timestamps_lltf = []
        complete_clusters_ht20 = []
        complete_cluster_timestamps_ht20 = []
        complete_clusters_ht40 = []
        complete_cluster_timestamps = []

        # Read wificonf to determine primary/secondary channel
        wificonf = self.get_wificonf()
        channel_primary = wificonf.get("channel-primary", None)
        channel_secondary = wificonf.get("channel-secondary", None)
        channel_secondary = -1 if channel_secondary == 2 else channel_secondary

        any_csi_count = 0
        stale_channel_counts: dict[tuple[int | None, int | None], int] = {}
        for cluster in clusters:
            cluster_channel_primary = cluster.get_primary_channel()
            cluster_channel_secondary = cluster.get_secondary_channel_relative()
            if channel_primary != cluster_channel_primary or channel_secondary != cluster_channel_secondary:
                stale_channel = (
                    cluster_channel_primary,
                    cluster_channel_secondary,
                )
                stale_channel_counts[stale_channel] = stale_channel_counts.get(stale_channel, 0) + 1
                continue

            completion = cluster.get_completion()[board_num] if board_num is not None else cluster.get_completion()
            if np.any(completion):
                any_csi_count = any_csi_count + 1

            if np.all(completion):
                cluster_timestamps = cluster.get_sensor_timestamps()[board_num] if board_num is not None else cluster.get_sensor_timestamps()
                complete_cluster_timestamps.append(cluster_timestamps)
                if cluster.has_lltf():
                    complete_clusters_lltf.append(cluster.deserialize_csi_lltf()[board_num] if board_num is not None else cluster.deserialize_csi_lltf())
                    complete_cluster_timestamps_lltf.append(cluster_timestamps)
                if cluster.has_ht20ltf():
                    complete_clusters_ht20.append(cluster.deserialize_csi_ht20ltf()[board_num] if board_num is not None else cluster.deserialize_csi_ht20ltf())
                    complete_cluster_timestamps_ht20.append(cluster_timestamps)
                if cluster.has_ht40ltf():
                    complete_clusters_ht40.append(cluster.deserialize_csi_ht40ltf()[board_num] if board_num is not None else cluster.deserialize_csi_ht40ltf())

        if stale_channel_counts:
            stale_channel_summary = ", ".join(f"primary {primary}, secondary {secondary}: {count}" for (primary, secondary), count in stale_channel_counts.items())
            self.logger.warning(
                "Skipping %d calibration cluster(s) with stale channel settings; " "expected primary %s and secondary %s, observed %s",
                sum(stale_channel_counts.values()),
                channel_primary,
                channel_secondary,
                stale_channel_summary,
            )

        if board_num is not None:
            self.logger.info(f"Board {self.boards[board_num].get_name()}: Collected {any_csi_count} calibration clusters:")
        else:
            self.logger.info(f"Collected {any_csi_count} calibration clusters:")
        self.logger.info(f"  - {len(complete_clusters_ht40)} complete clusters with HT40-LTF")
        self.logger.info(f"  - {len(complete_clusters_ht20)} complete clusters with HT20-LTF")
        self.logger.info(f"  - {len(complete_clusters_lltf)} complete clusters with L-LTF")

        if len(complete_clusters_ht20) > 0:
            # If we only have HT20 but no LLTF calibration, we can still proceed: Use corresponding subcarriers from HT20 for LLTF calibration
            if len(complete_clusters_lltf) == 0:
                self.logger.warning("No LLTF calibration clusters received, deriving LLTF calibration from HT20 calibration")
            complete_clusters_lltf.extend([util.extract_lltf_subcarriers_from_ht20(csi_ht20) for csi_ht20 in complete_clusters_ht20])
            complete_cluster_timestamps_lltf.extend(complete_cluster_timestamps_ht20)

        complete_cluster_count = len(complete_cluster_timestamps)
        calibration_error = None
        if any_csi_count < 5:
            calibration_error = "too few calibration packets were received"
        elif complete_cluster_count == 0:
            calibration_error = "no packet contained CSI from the complete calibrated array"
        elif len(complete_clusters_lltf) == 0:
            calibration_error = "no complete packet contained L-LTF or HT20-LTF CSI"

        if calibration_error is not None:
            raise CalibrationError(
                f"ESPARGOS calibration failed: {calibration_error}. "
                f"Received {any_csi_count} calibration packets with any CSI, "
                f"{complete_cluster_count} complete packets, "
                f"{len(complete_clusters_ht20)} complete HT20-LTF packets, "
                f"and {len(complete_clusters_lltf)} complete L-LTF/HT20-derived packets. "
                "Calibration needs several packets with CSI from the complete array and at least one complete L-LTF or HT20-LTF packet. "
                "Check signal level, RX gain, packet filtering, and whether all sensors are receiving the calibration/reference signal."
            )

        return (
            np.asarray(complete_clusters_lltf),
            np.asarray(complete_clusters_ht20),
            np.asarray(complete_clusters_ht40),
            np.asarray(complete_cluster_timestamps),
            np.asarray(complete_cluster_timestamps_lltf),
            channel_primary,
            channel_secondary,
        )

    def _compute_sensor_clock_offsets(self, complete_cluster_timestamps: np.ndarray) -> np.ndarray:
        """
        Compute clock offsets relative to the first sensor in the input scope.

        The input may contain the complete pool or one board. Its first axis is
        the calibration-cluster axis; all remaining axes describe the sensors
        whose clocks share the selected reference.

        :param complete_cluster_timestamps: Per-sensor timestamps in seconds.
        :return: Mean clock offsets with the cluster axis removed.
        """
        sensor_timestamps = np.asarray(
            complete_cluster_timestamps,
            dtype=np.float64,
        )
        if sensor_timestamps.ndim < 2:
            raise ValueError(
                "complete_cluster_timestamps must contain a cluster axis and "
                "at least one sensor axis"
            )
        if len(sensor_timestamps) == 0:
            return np.full(sensor_timestamps.shape[1:], np.nan, dtype=np.float64)

        reference_slice = (slice(None),) + (slice(0, 1),) * (
            sensor_timestamps.ndim - 1
        )
        sensor_clock_offsets = sensor_timestamps - sensor_timestamps[reference_slice]
        return np.mean(sensor_clock_offsets, axis=0)

    def calibrate(
        self,
        per_board=True,
        duration=2,
        cable_lengths=None,
        cable_velocity_factors=None,
        run_in_thread=True,
    ):
        """
        Run calibration for a specified duration.

        :param per_board: True to calibrate each board against its own phase and
                          clock reference. Set to False only when every board
                          receives the same reference packets and shares a
                          common clock.
        :param duration: The duration in seconds for which calibration should be run
        :param cable_lengths: The lengths of the feeder cables that distribute the clock and phase calibration signal to the ESPARGOS boards, in meters.
                              Only needed for phase-coherent multi-board setups, omit if all cables have the same length.
        :param cable_velocity_factors: The velocity factors of the feeder cables that distribute the clock and phase calibration signal to the ESPARGOS boards
                                       Must be the same length as :code:`cable_lengths`, and all entries should be in the range [0, 1].
        :param run_in_thread: If True, the pool handling will be performed in the current thread. Set to False in case the pool is already running in a separate thread (e.g., backlog is already active).
        """
        self._clear_cluster_cache(_CACHE_CALIBRATION)

        # Back up and clear MAC filter
        previous_mac_filter = self.get_mac_filter()
        previous_rfswitch_state = self.get_rfswitch()

        try:
            self.clear_mac_filter()

            # Enable calibration mode
            self.logger.info("Starting calibration")
            self.set_rfswitch(sensor.RFSwitchState.SENSOR_RFSWITCH_REFERENCE)

            # Run calibration for specified duration
            start = time.monotonic()
            while time.monotonic() - start < duration:
                if run_in_thread:
                    remaining = max(0.0, duration - (time.monotonic() - start))
                    self.run(timeout=min(0.05, remaining))
                else:
                    time.sleep(0.01)
        finally:
            # Disable calibration mode
            self.logger.info("Finished calibration")
            self.set_rfswitch(previous_rfswitch_state)
            self.set_mac_filter(previous_mac_filter)
            self._clear_cluster_cache(_CACHE_OTA)

        if per_board:
            phase_calibrations_lltf = []
            phase_calibrations_ht20 = []
            phase_calibrations_ht40 = []
            phase_calibrations_he20 = []
            sensor_clock_offsets = []
            channel_primary = None
            channel_secondary = None

            for board_num in range(len(self.boards)):
                (
                    complete_clusters_lltf,
                    complete_clusters_ht20,
                    complete_clusters_ht40,
                    complete_cluster_timestamps,
                    complete_cluster_timestamps_lltf,
                    board_channel_primary,
                    board_channel_secondary,
                ) = self._clusters_to_calibration(board_num)

                if channel_primary is None:
                    channel_primary = board_channel_primary
                    channel_secondary = board_channel_secondary
                elif (
                    channel_primary != board_channel_primary
                    or channel_secondary != board_channel_secondary
                ):
                    raise CalibrationError(
                        "ESPARGOS calibration failed: boards reported different "
                        "calibration channels"
                    )

                phase_calibrations_lltf.append(
                    util.csi_interp_eigenvec_per_subcarrier(np.asarray(complete_clusters_lltf))
                    if len(complete_clusters_lltf) > 0
                    else np.full(
                        self.get_shape()[1:] + (csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL,),
                        np.nan,
                    )
                )
                phase_calibrations_ht20.append(
                    util.csi_interp_eigenvec_per_subcarrier(np.asarray(complete_clusters_ht20))
                    if len(complete_clusters_ht20) > 0
                    else np.full(
                        self.get_shape()[1:] + (csi_packet.HT_COEFFICIENTS_PER_CHANNEL,),
                        np.nan,
                    )
                )
                phase_calibrations_ht40.append(
                    util.csi_interp_eigenvec_per_subcarrier(np.asarray(complete_clusters_ht40))
                    if len(complete_clusters_ht40) > 0
                    else np.full(
                        self.get_shape()[1:] + (csi_packet.HT_COEFFICIENTS_PER_CHANNEL * 2 + csi_packet.HT40_GAP_SUBCARRIERS,),
                        np.nan,
                    )
                )
                phase_calibrations_he20.append(
                    util.derive_he20_calibration_from_lltf(
                        complete_clusters_lltf[:, np.newaxis, ...],
                        complete_cluster_timestamps_lltf[:, np.newaxis, ...],
                        board_channel_secondary,
                    )[0]
                )
                sensor_clock_offsets.append(
                    self._compute_sensor_clock_offsets(
                        complete_cluster_timestamps,
                    )
                )

            self.stored_calibration = calibration.CSICalibration(
                self.boards,
                channel_primary,
                channel_secondary,
                np.asarray(phase_calibrations_lltf),
                np.asarray(phase_calibrations_ht20),
                np.asarray(phase_calibrations_ht40),
                np.asarray(phase_calibrations_he20),
                sensor_clock_offsets=np.asarray(sensor_clock_offsets),
                clock_scope=(
                    calibration.ClockReferenceScope.POOL
                    if len(self.boards) == 1
                    else calibration.ClockReferenceScope.PER_BOARD
                ),
            )

        else:
            (
                complete_clusters_lltf,
                complete_clusters_ht20,
                complete_clusters_ht40,
                complete_cluster_timestamps,
                complete_cluster_timestamps_lltf,
                channel_primary,
                channel_secondary,
            ) = self._clusters_to_calibration()
            sensor_clock_offsets = self._compute_sensor_clock_offsets(
                complete_cluster_timestamps
            )
            phase_calibration_he20 = util.derive_he20_calibration_from_lltf(
                complete_clusters_lltf,
                complete_cluster_timestamps_lltf,
                channel_secondary,
            )

            phase_calibrations_lltf = util.csi_interp_eigenvec_per_subcarrier(np.asarray(complete_clusters_lltf)) if len(complete_clusters_lltf) > 0 else np.full(self.get_shape() + (csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL,), np.nan)
            phase_calibrations_ht20 = util.csi_interp_eigenvec_per_subcarrier(np.asarray(complete_clusters_ht20)) if len(complete_clusters_ht20) > 0 else np.full(self.get_shape() + (csi_packet.HT_COEFFICIENTS_PER_CHANNEL,), np.nan)
            phase_calibration_ht40 = (
                util.csi_interp_eigenvec_per_subcarrier(np.asarray(complete_clusters_ht40))
                if len(complete_clusters_ht40) > 0
                else np.full(
                    self.get_shape() + (csi_packet.HT_COEFFICIENTS_PER_CHANNEL * 2 + csi_packet.HT40_GAP_SUBCARRIERS,),
                    np.nan,
                )
            )

            # Each antenna just gets a delayed and phase-shifted version of the reference signal,
            # so frequency response is just a complex sinusoid over subcarrier axis.
            # Fit optimal complex sinusoid to the CSI of each antenna across subcarriers to extract the phase shift and delay, which we can then use for calibration.
            phase_calibrations_lltf = util.fit_complex_sinusoid(phase_calibrations_lltf)
            phase_calibrations_ht20 = util.fit_complex_sinusoid(phase_calibrations_ht20)
            phase_calibration_ht40 = util.fit_complex_sinusoid(phase_calibration_ht40)

            self.stored_calibration = calibration.CSICalibration(
                self.boards,
                channel_primary,
                channel_secondary,
                phase_calibrations_lltf,
                phase_calibrations_ht20,
                phase_calibration_ht40,
                phase_calibration_he20,
                sensor_clock_offsets=sensor_clock_offsets,
                clock_scope=calibration.ClockReferenceScope.POOL,
                board_cable_lengths=cable_lengths,
                board_cable_vfs=cable_velocity_factors,
            )

    def get_calibration(self):
        """
        Get the stored calibration values.

        :return: The stored calibration values as a :class:`.calibration.CSICalibration` object
        """
        return self.stored_calibration

    def _get_cluster_cache_name(
        self,
        board_num: int,
        sensor_message: sensor.SensorMessage,
    ) -> str:
        stream_packet = sensor_message.payload
        if isinstance(stream_packet, csi_packet.CSIPacket):
            return _CACHE_CALIBRATION if stream_packet.is_calib else _CACHE_OTA
        if isinstance(stream_packet, radar_packet.RadarTxReportPacket):
            return _CACHE_OTA
        raise TypeError(f"Unsupported CSIPool sensor-message payload: {type(stream_packet).__name__}")

    def _get_cluster_key(
        self,
        board_num: int,
        sensor_message: sensor.SensorMessage,
    ) -> wifi.WiFiFrameKey:
        return wifi.WiFiFrameKey.from_packet(sensor_message.payload)

    def _create_cluster(
        self,
        cache_name: str,
        cluster_key: wifi.WiFiFrameKey,
        board_num: int,
        first_message: sensor.SensorMessage,
    ) -> csi_cluster.CSICluster:
        return csi_cluster.CSICluster(cluster_key, self.board_revisions)

    def _on_cluster_updated(
        self,
        cache_name: str,
        cluster_key: wifi.WiFiFrameKey,
        sensor_cluster: SensorCluster,
    ) -> bool:
        if not isinstance(sensor_cluster, csi_cluster.CSICluster):
            raise TypeError(f"CSIPool received unexpected cluster type: {type(sensor_cluster).__name__}")

        if cache_name == _CACHE_CALIBRATION:
            if self.emit_calibration_csi:
                self._try_callbacks(sensor_cluster)
            return False

        all_callbacks_fired = self._try_callbacks(sensor_cluster)
        return all_callbacks_fired and np.any(sensor_cluster.get_completion())

    def _get_cluster_cache_timeout(self, cache_name: str) -> float | None:
        return self.ota_cache_timeout if cache_name == _CACHE_OTA else None
