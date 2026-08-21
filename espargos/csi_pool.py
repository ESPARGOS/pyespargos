#!/usr/bin/env python

"""Configure Wi-Fi reception and combine per-sensor CSI into packet clusters.

Every sensor reports CSI independently, while applications generally want one
array-shaped measurement for each received Wi-Fi packet. :class:`CSIPool`
associates reports using their MAC addresses and sequence control field, stores
them in :class:`.CSICluster` objects, and emits those clusters when a callback's
completion requirement is met. It also provides the pool-wide Wi-Fi, radar, and
calibration API.
"""

from typing import Callable
import numpy as np
import time

from . import csi_calibration
from . import csi_association
from . import board
from . import csi_processing
from . import csi_cluster
from . import csi_packet
from . import radar_packet
from . import sensor
from . import radar
from . import wifi
from .pool import Pool
from .sensor_calibration import ClockReferenceScope
from .sensor_cluster import SensorCluster

__all__ = ["CSIPool", "CalibrationError"]


class CalibrationError(RuntimeError):
    """Raised when ESPARGOS calibration cannot collect usable calibration CSI."""


# WiFi configuration fields that legitimately differ between boards. Pool-wide
# reads and writes must leave these board-local calibration settings untouched.
WIFI_CONFIG_PER_BOARD_KEYS = {
    "calib-source",
    "calib-mode",
    "calib-txpower",
    "calib-interval",
}

_CACHE_OTA = "ota"
_CACHE_CALIBRATION = "calibration"


class CSIPool(Pool):
    """Manage Wi-Fi features and assemble CSI for each received Wi-Fi packet."""

    def __init__(
        self,
        boards: list[board.Board],
        ota_cache_timeout=5,
        reference_generator_boards=None,
        gain_phase_compensation=True,
    ):
        """
        Constructor for the CSIPool class.

        :param boards: A list of ESPARGOS boards that belong to the pool
        :param ota_cache_timeout: Optional. The timeout in seconds after which over-the-air CSI data is considered stale and discarded
                                  if the cluster is not complete
        :param reference_generator_boards: Optional. In some multi-board setups, the calibration signal is provided by (a) separate ESPARGOS device(s)
                              that is / are not part of the pool (only controller is used to generate packets, sensors not used).
                              If provided, sends calibration command to these boards, which will then generate the calibration signal
                              during calibration phase.
        :param gain_phase_compensation: Whether to correct the deterministic
                                        phase jumps caused when AGC switches
                                        analog gain elements. When enabled
                                        (the default), each sensor's reported
                                        gain-table index selects a fixed,
                                        hardware-characterized phase correction
                                        that is applied whenever CSI is
                                        deserialized. The correction rotates
                                        phase without changing amplitude;
                                        disabling it returns CSI without this
                                        additional rotation.
        """
        super().__init__(boards)
        self._reference_generator_boards = reference_generator_boards if reference_generator_boards is not None else []

        self._ota_cache_timeout = ota_cache_timeout
        self._emit_calibration_csi = False
        self._gain_phase_enabled = bool(gain_phase_compensation)
        self._association_last_timestamp_us: dict[tuple[int, int], int] = {}
        self._association_epoch = 0
        self._association_serial = 0
        self._association_stats = {
            "observations": 0,
            "dropped_without_calibration": 0,
            "dropped_without_timestamp": 0,
            "dropped_ambiguous": 0,
            "clusters_created": 0,
            "observations_matched": 0,
            "max_timestamp_residual_ns": 0,
        }

        for board_index, board_obj in enumerate(self.boards):
            wifi_rx = board_obj.wifi_rx
            wifi_tx = board_obj.wifi_tx
            self._subscribe_sensor_messages(
                board_index,
                wifi_rx,
                wifi_rx.subscribe_csi,
            )
            self._subscribe_sensor_messages(
                board_index,
                wifi_tx,
                wifi_tx.subscribe_reports,
            )

        self._calibration: csi_calibration.CSICalibration | None = None

    def set_rf_switch(self, state: sensor.RFSwitchState):
        """
        Set RF switch state for all boards in the pool.

        :param state: The RF switch state to set, must be one of :class:`sensor.RFSwitchState`
        """
        for board in self.boards + self._reference_generator_boards:
            board.wifi_rx.set_rf_switch(state)

    def get_rf_switch(self) -> sensor.RFSwitchState:
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

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRxCapability.set_mac_filter` for each board.
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

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRxCapability.get_mac_filter` for each board.
        """
        filters = [b.wifi_rx.get_mac_filter() for b in self.boards]
        return self._reconcile_across_boards(
            filters,
            "MAC filter",
            lambda value: [board.wifi_rx.set_mac_filter(value) for board in self.boards],
        )

    def get_csi_acquisition_config(self) -> dict:
        """
        Return CSI acquire config, reconciling boards when needed.
        """
        cfgs = [b.wifi_rx.get_csi_acquisition_config() for b in self.boards]
        return self._reconcile_across_boards(
            cfgs,
            "CSI acquire config",
            lambda value: [board.wifi_rx.set_csi_acquisition_config(value) for board in self.boards],
        )

    def set_csi_acquisition_config(self, config: dict):
        """
        Set CSI acquisition configuration on all boards in this pool and sanity-check that all boards
        end up with the same config.

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRxCapability.set_csi_acquisition_config` for each board.
        For the expected JSON/dict format, refer to that method's documentation.

        :param config: CSI acquisition configuration dict to apply to all boards.
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        for b in self.boards:
            b.wifi_rx.set_csi_acquisition_config(config)
        _ = self.get_csi_acquisition_config()

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

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRxCapability.set_gain_settings` for each board.
        Values may be scalars, board-local ``(row, column)`` arrays applied to every
        board, or pool-wide ``(board, row, column)`` arrays.

        :param settings: Gain settings dict to apply to all boards.
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        per_board_settings = [dict() for _ in self.boards]
        for key, value in settings.items():
            array = np.asarray(value)
            if array.shape == self.shape:
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

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRxCapability.set_channel_overrides` for each board.
        For the expected JSON/dict format, refer to that method's documentation.

        :param settings: Per-sensor WiFi channel override settings dict to apply to all boards.
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        for b in self.boards:
            b.wifi_rx.set_channel_overrides(settings)
        _ = self.get_wifi_channel_overrides()
        self._invalidate_association_timebase("per-sensor Wi-Fi channels changed")

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

    def get_wifi_config(self) -> dict:
        """
        Return WiFi config, reconciling pool-wide settings when boards disagree.

        Board-local calibration fields are excluded from both comparison and
        reconciliation, so each board retains its own values.
        """
        wifi_configs = [b.wifi_rx.get_config() for b in self.boards]
        return self._reconcile_across_boards(
            wifi_configs,
            "WiFi config",
            lambda value: [board.wifi_rx.set_config(value) for board in self.boards],
            ignore_keys=WIFI_CONFIG_PER_BOARD_KEYS,
        )

    def set_wifi_config(self, wifi_config: dict):
        """
        Set WiFi config on all boards and sanity-check resulting configs match across boards.

        This is forwarded to :meth:`pyespargos.board_wifi_rx.WiFiRxCapability.set_config` for each board.
        For the expected JSON/dict format, refer to that method's documentation.

        Fields listed in :data:`WIFI_CONFIG_PER_BOARD_KEYS` are ignored and not
        propagated because they may legitimately differ between boards. Set
        those directly through :meth:`pyespargos.board_wifi_rx.WiFiRxCapability.set_config`.

        :param wifi_config: WiFi configuration dict to apply to all boards.
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        wifi_config = {key: value for key, value in wifi_config.items() if key not in WIFI_CONFIG_PER_BOARD_KEYS}
        for b in self.boards:
            b.wifi_rx.set_config(wifi_config)
        _ = self.get_wifi_config()
        self._invalidate_association_timebase("Wi-Fi configuration changed")

    def reboot(self):
        """
        Trigger a reboot on all boards in the pool.
        """
        super().reboot()
        for board_obj in self._reference_generator_boards:
            board_obj.reboot()

    def add_csi_callback(
        self,
        callback: Callable[[csi_cluster.CSICluster], None],
        callback_predicate: Callable[[csi_cluster.CSICluster], bool] = None,
    ):
        """
        Register callback function that is invoked whenever a new CSI cluster is completed.

        :param callback: The function to call, gets an instance of :class:`.csi_cluster.CSICluster`
        :param callback_predicate: A function with signature :code:`(csi_cluster)` that defines the conditions under which
            clustered CSI is regarded as completed and thus provided to the callback.
            If :code:`callback_predicate` returns true, clustered CSI is regarded as completed.
            If no predicate is provided, the default behavior is to trigger the callback when CSI has been received
            from all sensors on all boards. By default, callbacks receive over-the-air/radar CSI only. Enable
            :attr:`emit_calibration_csi` to also emit calibration CSI (from internal reference generators)
            through this same callback path.
        :return: A callback handle that can be passed to :meth:`remove_csi_callback`
        """
        return self.add_cluster_callback(callback, callback_predicate)

    def remove_csi_callback(self, callback) -> bool:
        """
        Remove a CSI callback previously returned by :meth:`add_csi_callback`.

        :param callback: Callback handle returned by :meth:`add_csi_callback`
        :return: True if the callback was registered and removed, False otherwise
        """
        return self.remove_cluster_callback(callback)

    def replace_csi_callback(
        self,
        callback,
        callback_function: Callable[[csi_cluster.CSICluster], None],
        callback_predicate: Callable[[csi_cluster.CSICluster], bool] = None,
    ):
        """Atomically replace a registered CSI callback.

        :param callback: Existing handle returned by :meth:`add_csi_callback`
        :param callback_function: Replacement callback function
        :param callback_predicate: Replacement completion predicate
        :return: New callback handle
        :raises ValueError: If ``callback`` is no longer registered
        """

        return self.replace_cluster_callback(callback, callback_function, callback_predicate)

    @property
    def emit_calibration_csi(self) -> bool:
        """Return whether calibration CSI is emitted through normal callbacks."""

        return self._emit_calibration_csi

    @emit_calibration_csi.setter
    def emit_calibration_csi(self, enabled: bool):
        """
        Control whether calibration CSI clusters are emitted through normal CSI callbacks.

        Calibration clusters remain marked as calibration packets via
        :attr:`espargos.csi_cluster.CSICluster.is_calibration`.
        """
        self._emit_calibration_csi = bool(enabled)

    @property
    def gain_phase_compensation(self) -> bool:
        """Whether deserialized CSI is corrected for gain-element phase jumps."""

        return self._gain_phase_enabled

    @gain_phase_compensation.setter
    def gain_phase_compensation(self, enabled: bool):
        """Enable or disable gain-element phase correction for new CSI."""

        enabled = bool(enabled)
        if enabled == self._gain_phase_enabled:
            return
        self._gain_phase_enabled = enabled
        self._clear_cluster_cache(_CACHE_OTA)
        self._clear_cluster_cache(_CACHE_CALIBRATION)

    def _clusters_to_calibration(self, board_index=None):
        """
        Convert the collected calibration clusters into per-antenna calibration offsets.

        Collects the complete calibration clusters per CSI format and estimates
        the per-antenna timing and phase offsets from the widest available
        format (a wider measurement band yields more accurate timing offsets).

        :param board_index: If provided, only process calibration clusters for the specified board index
        :return: Tuple of per-antenna timing offsets, phase offsets, the primary channel,
                 and the relative secondary channel position.
        """
        clusters = self._get_cluster_cache_snapshot(_CACHE_CALIBRATION)

        # Collection of complete clusters (= reference CSI data from all antennas available): L-LTF, HT20-LTF, and HT40-LTF
        complete_clusters_lltf = []
        complete_cluster_timestamps_lltf = []
        complete_clusters_ht20 = []
        complete_cluster_timestamps_ht20 = []
        complete_clusters_ht40 = []
        complete_cluster_timestamps_ht40 = []
        complete_cluster_timestamps = []

        # Read Wi-Fi configuration to determine primary/secondary channel
        wifi_config = self.get_wifi_config()
        channel_primary = wifi_config.get("channel-primary", None)
        channel_secondary = wifi_config.get("channel-secondary", None)
        channel_secondary = -1 if channel_secondary == 2 else channel_secondary

        any_csi_count = 0
        stale_channel_counts: dict[tuple[int | None, int | None], int] = {}
        for cluster in clusters:
            cluster_channel_primary = cluster.primary_channel
            cluster_channel_secondary = cluster.secondary_channel_relative
            if channel_primary != cluster_channel_primary or channel_secondary != cluster_channel_secondary:
                stale_channel = (
                    cluster_channel_primary,
                    cluster_channel_secondary,
                )
                stale_channel_counts[stale_channel] = stale_channel_counts.get(stale_channel, 0) + 1
                continue

            completion = cluster.completion[board_index] if board_index is not None else cluster.completion
            if np.any(completion):
                any_csi_count = any_csi_count + 1

            if np.all(completion):
                cluster_timestamps = cluster.sensor_timestamps[board_index] if board_index is not None else cluster.sensor_timestamps
                complete_cluster_timestamps.append(cluster_timestamps)
                if cluster.has_lltf:
                    complete_clusters_lltf.append(cluster.deserialize_csi_lltf()[board_index] if board_index is not None else cluster.deserialize_csi_lltf())
                    complete_cluster_timestamps_lltf.append(cluster_timestamps)
                if cluster.has_ht20ltf:
                    complete_clusters_ht20.append(cluster.deserialize_csi_ht20ltf()[board_index] if board_index is not None else cluster.deserialize_csi_ht20ltf())
                    complete_cluster_timestamps_ht20.append(cluster_timestamps)
                if cluster.has_ht40ltf:
                    complete_clusters_ht40.append(cluster.deserialize_csi_ht40ltf()[board_index] if board_index is not None else cluster.deserialize_csi_ht40ltf())
                    complete_cluster_timestamps_ht40.append(cluster_timestamps)

        if stale_channel_counts:
            stale_channel_summary = ", ".join(f"primary {primary}, secondary {secondary}: {count}" for (primary, secondary), count in stale_channel_counts.items())
            self._logger.warning(
                "Skipping %d calibration cluster(s) with stale channel settings; " "expected primary %s and secondary %s, observed %s",
                sum(stale_channel_counts.values()),
                channel_primary,
                channel_secondary,
                stale_channel_summary,
            )

        if board_index is not None:
            self._logger.info(f"Board {self.boards[board_index].name}: Collected {any_csi_count} calibration clusters:")
        else:
            self._logger.info(f"Collected {any_csi_count} calibration clusters:")
        self._logger.info(f"  - {len(complete_clusters_ht40)} complete clusters with HT40-LTF")
        self._logger.info(f"  - {len(complete_clusters_ht20)} complete clusters with HT20-LTF")
        self._logger.info(f"  - {len(complete_clusters_lltf)} complete clusters with L-LTF")

        complete_cluster_count = len(complete_cluster_timestamps)
        format_count = len(complete_clusters_lltf) + len(complete_clusters_ht20) + len(complete_clusters_ht40)
        calibration_error = None
        if any_csi_count < 5:
            calibration_error = "too few calibration packets were received"
        elif complete_cluster_count == 0:
            calibration_error = "no packet contained CSI from the complete calibrated array"
        elif format_count == 0:
            calibration_error = "no complete packet contained L-LTF, HT20-LTF or HT40-LTF CSI"

        if calibration_error is not None:
            raise CalibrationError(
                f"ESPARGOS calibration failed: {calibration_error}. "
                f"Received {any_csi_count} calibration packets with any CSI, "
                f"{complete_cluster_count} complete packets, "
                f"{len(complete_clusters_lltf)} complete L-LTF, "
                f"{len(complete_clusters_ht20)} complete HT20-LTF, "
                f"and {len(complete_clusters_ht40)} complete HT40-LTF packets. "
                "Calibration needs several packets with CSI from the complete array. "
                "Check signal level, RX gain, packet filtering, and whether all sensors are receiving the calibration/reference signal."
            )

        # Estimate the offsets from the widest available format, since a wider
        # measurement band yields more accurate timing offsets
        clusters_by_format = {
            "ht40": (np.asarray(complete_clusters_ht40), np.asarray(complete_cluster_timestamps_ht40)),
            "ht20": (np.asarray(complete_clusters_ht20), np.asarray(complete_cluster_timestamps_ht20)),
            "lltf": (np.asarray(complete_clusters_lltf), np.asarray(complete_cluster_timestamps_lltf)),
        }
        for csi_format, (format_clusters, format_timestamps) in clusters_by_format.items():
            if len(format_clusters) > 0:
                break

        self._logger.info(f"Estimating calibration offsets from {len(format_clusters)} {csi_format} cluster(s)")
        frequencies, valid = csi_processing.get_csi_sto_correction_frequencies(csi_format, channel_secondary)

        # Per-board calibration data has no board axis; add and remove it around the estimation
        if board_index is not None:
            format_clusters = format_clusters[:, np.newaxis]
            format_timestamps = format_timestamps[:, np.newaxis]

        timing_offsets, phase_offsets = csi_processing.estimate_phase_time_offsets(format_clusters, format_timestamps, frequencies, valid)
        if board_index is not None:
            timing_offsets, phase_offsets = timing_offsets[0], phase_offsets[0]

        return timing_offsets, phase_offsets, channel_primary, channel_secondary

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
                              Only applicable to pool-wide calibration (:code:`per_board=False`) of phase-coherent multi-board setups; omit if all cables
                              have the same length. In per-board mode, the cable delay is common to all sensors of a board and cancels within the board's
                              own reference scope, so cable compensation does not apply.
        :param cable_velocity_factors: The velocity factors of the feeder cables that distribute the clock and phase calibration signal to the ESPARGOS boards
                                       Must be the same length as :code:`cable_lengths`, and all entries should be in the range [0, 1].
        :param run_in_thread: If True, the pool handling will be performed in the current thread. Set to False in case the pool is already running in a separate thread (e.g., backlog is already active).
        """
        if per_board and cable_lengths is not None:
            self._logger.warning("Cable lengths are ignored in per-board calibration mode: cable delays cancel within each board's own reference scope")

        # A channel retune can change the sensors' relative clock offsets. Do
        # not use the previous calibration to associate the reference packets
        # from which its replacement is being estimated.
        self._invalidate_association_timebase("new calibration started", warn=False)
        self._clear_cluster_cache(_CACHE_CALIBRATION)

        # Back up and clear MAC filter
        previous_mac_filter = self.get_mac_filter()
        previous_rf_switch_state = self.get_rf_switch()

        try:
            self.clear_mac_filter()

            # Enable calibration mode
            self._logger.info("Starting calibration")
            self.set_rf_switch(sensor.RFSwitchState.SENSOR_RFSWITCH_REFERENCE)

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
            self._logger.info("Finished calibration")
            self.set_rf_switch(previous_rf_switch_state)
            self.set_mac_filter(previous_mac_filter)
            self._clear_cluster_cache(_CACHE_OTA)

        # Each antenna receives a delayed and phase-shifted version of the reference
        # signal, so calibration reduces to one timing offset and one phase offset
        # per antenna, estimated from the L-LTF clusters and their timestamps. The
        # per-format calibration vectors are synthesized from these offsets by
        # CSICalibration, independently of which formats the reference provided.
        if per_board:
            timing_offsets = []
            phase_offsets = []
            channel_primary = None
            channel_secondary = None

            for board_index in range(len(self.boards)):
                (
                    board_timing_offsets,
                    board_phase_offsets,
                    board_channel_primary,
                    board_channel_secondary,
                ) = self._clusters_to_calibration(board_index)

                if channel_primary is None:
                    channel_primary = board_channel_primary
                    channel_secondary = board_channel_secondary
                elif channel_primary != board_channel_primary or channel_secondary != board_channel_secondary:
                    raise CalibrationError("ESPARGOS calibration failed: boards reported different " "calibration channels")

                timing_offsets.append(board_timing_offsets)
                phase_offsets.append(board_phase_offsets)

            # No cable compensation in per-board mode: each board is calibrated
            # against its own reference, and the distribution cable delay is common
            # to all sensors of a board, so it cancels within the board's scope.
            self._calibration = csi_calibration.CSICalibration(
                self.boards,
                channel_primary,
                channel_secondary,
                np.asarray(timing_offsets),
                np.asarray(phase_offsets),
                clock_scope=(csi_calibration.ClockReferenceScope.POOL if len(self.boards) == 1 else csi_calibration.ClockReferenceScope.PER_BOARD),
            )

        else:
            timing_offsets, phase_offsets, channel_primary, channel_secondary = self._clusters_to_calibration()

            self._calibration = csi_calibration.CSICalibration(
                self.boards,
                channel_primary,
                channel_secondary,
                timing_offsets,
                phase_offsets,
                clock_scope=csi_calibration.ClockReferenceScope.POOL,
                board_cable_lengths=cable_lengths,
                board_cable_vfs=cable_velocity_factors,
            )

        self._association_epoch += 1

    @property
    def calibration(self) -> csi_calibration.CSICalibration | None:
        """
        Get the stored calibration values.

        :return: The stored calibration values as a :class:`.csi_calibration.CSICalibration` object
        """
        return self._calibration

    @property
    def association_stats(self) -> dict:
        """Return counters for generic frame-identity association."""

        return dict(self._association_stats)

    @property
    def timestamp_association_available(self) -> bool:
        """Whether calibration provides one clock domain for this pool."""

        calibration = self._calibration
        return calibration is not None and (len(self.boards) == 1 or calibration.clock_scope == ClockReferenceScope.POOL)

    def _invalidate_association_timebase(self, reason: str, *, warn: bool = True) -> None:
        """Invalidate calibration and discard timestamp-associated clusters."""

        had_calibration = self._calibration is not None
        self._calibration = None
        self._association_epoch += 1
        self._association_last_timestamp_us.clear()
        with self._cluster_lock:
            cache = self._cluster_caches.get(_CACHE_OTA)
            if cache is not None:
                for key in tuple(cache):
                    if isinstance(key, csi_association.FrameIdentity) and key.timestamp_required:
                        cache.pop(key, None)
        if had_calibration and warn:
            self._logger.warning("Timestamp-based frame association disabled: %s; run calibration again", reason)

    def _observe_sensor_clock(
        self,
        board_index: int,
        sensor_message: sensor.SensorMessage,
    ) -> None:
        """Invalidate a stale REFTX timebase when a sensor clock restarts."""

        stream_packet = sensor_message.payload
        if not isinstance(stream_packet, csi_packet.CSIPacket):
            return
        sensor_id = (board_index, sensor_message.antenna_id)
        timestamp_us = int(stream_packet.global_timestamp_us)
        previous = self._association_last_timestamp_us.get(sensor_id)
        if previous is not None and timestamp_us + 1_000_000 < previous:
            self._invalidate_association_timebase(f"sensor {board_index}/{sensor_message.antenna_id} timestamp restarted")
        latest = self._association_last_timestamp_us.get(sensor_id)
        if latest is None or timestamp_us > latest:
            self._association_last_timestamp_us[sensor_id] = timestamp_us

    def _frame_identity(
        self,
        board_index: int,
        sensor_message: sensor.SensorMessage,
    ) -> csi_association.FrameIdentity | None:
        """Build one generic identity, or decline unsafe control association."""

        self._association_stats["observations"] += 1
        stream_packet = sensor_message.payload
        signature = csi_association.FrameSignature.from_packet(stream_packet)
        calibration = self._calibration
        calibration_matches = self.timestamp_association_available and signature.channel == calibration.channel_primary and signature.secondary_channel_relative == calibration.channel_secondary_relative
        if not calibration_matches:
            if csi_association.is_control_frame(stream_packet):
                self._association_stats["dropped_without_calibration"] += 1
                return None
            self._association_serial += 1
            return csi_association.FrameIdentity(
                instance_id=self._association_serial,
                signature=signature,
            )

        row, column = self.board_revisions[board_index].antenna_id_to_row_col(sensor_message.antenna_id)
        reference_timestamp_ns = csi_association.frame_reference_timestamp_ns(
            stream_packet,
            calibration.timing_offsets[board_index, row, column],
        )
        if reference_timestamp_ns is None:
            self._association_stats["dropped_without_timestamp"] += 1
            return None
        self._association_serial += 1
        return csi_association.FrameIdentity(
            instance_id=self._association_serial,
            signature=signature,
            timestamp_ns=reference_timestamp_ns,
            timestamp_required=True,
            calibration_epoch=self._association_epoch,
        )

    def _get_cluster_cache_name(
        self,
        board_index: int,
        sensor_message: sensor.SensorMessage,
    ) -> str:
        stream_packet = sensor_message.payload
        if isinstance(stream_packet, csi_packet.CSIPacket):
            return _CACHE_CALIBRATION if stream_packet.is_calibration else _CACHE_OTA
        if isinstance(stream_packet, radar_packet.RadarTxReportPacket):
            return _CACHE_OTA
        raise TypeError(f"Unsupported CSIPool sensor-message payload: {type(stream_packet).__name__}")

    def _get_cluster_key(
        self,
        board_index: int,
        sensor_message: sensor.SensorMessage,
    ) -> wifi.WiFiFrameKey | csi_association.FrameIdentity | None:
        self._observe_sensor_clock(board_index, sensor_message)
        stream_packet = sensor_message.payload
        if isinstance(stream_packet, csi_packet.CSIPacket):
            return self._frame_identity(board_index, sensor_message)
        return wifi.WiFiFrameKey.from_packet(stream_packet)

    def _resolve_cluster_key(
        self,
        cache_name,
        proposed_key,
        cache,
        board_index,
        sensor_message,
    ):
        candidates = []
        for existing_key, cluster in cache.items():
            match = None
            if isinstance(proposed_key, csi_association.FrameIdentity):
                anchor = cluster.frame_identity
                if isinstance(anchor, csi_association.FrameIdentity):
                    match = anchor.match(proposed_key)
                elif existing_key == proposed_key.frame_key:
                    # A radar TX report may have created the cluster before its
                    # first CSI observation. It provides no RX timestamp, so it
                    # can only be attached when its conventional frame key is
                    # unique; the first CSI then becomes the timestamp anchor.
                    match = csi_association.FrameIdentityMatch(None)
            elif isinstance(existing_key, csi_association.FrameIdentity):
                # Radar TX reports are auxiliary observations without an RX
                # timestamp. Attach one only to a unique matching CSI cluster.
                if existing_key.frame_key == proposed_key:
                    match = csi_association.FrameIdentityMatch(None)
            else:
                continue

            if match is None:
                continue
            position = cluster.get_sensor_position(board_index, sensor_message.antenna_id)
            if isinstance(sensor_message.payload, csi_packet.CSIPacket) and cluster.completion[position]:
                continue
            if isinstance(sensor_message.payload, radar_packet.RadarTxReportPacket) and cluster.has_radar_tx_report:
                continue
            candidates.append((match, existing_key, cluster))

        if len(candidates) > 1:
            self._association_stats["dropped_ambiguous"] += 1
            return None
        if len(candidates) == 1:
            match, existing_key, cluster = candidates[0]
            if isinstance(proposed_key, csi_association.FrameIdentity) and not isinstance(cluster.frame_identity, csi_association.FrameIdentity):
                cluster.frame_identity = proposed_key
            self._association_stats["observations_matched"] += 1
            if match.timestamp_residual_ns is not None:
                self._association_stats["max_timestamp_residual_ns"] = max(
                    self._association_stats["max_timestamp_residual_ns"],
                    match.timestamp_residual_ns,
                )
            return existing_key

        if proposed_key in cache:
            if isinstance(proposed_key, csi_association.FrameIdentity) and proposed_key.timestamp_required:
                self._association_stats["dropped_ambiguous"] += 1
                return None
            return proposed_key

        self._association_stats["clusters_created"] += 1
        return proposed_key

    def _create_cluster(
        self,
        cache_name: str,
        cluster_key: wifi.WiFiFrameKey | csi_association.FrameIdentity,
        board_index: int,
        first_message: sensor.SensorMessage,
    ) -> csi_cluster.CSICluster:
        frame_key = cluster_key.frame_key if isinstance(cluster_key, csi_association.FrameIdentity) else cluster_key
        return csi_cluster.CSICluster(
            frame_key,
            self.board_revisions,
            gain_phase_compensation=self._gain_phase_enabled,
            frame_identity=cluster_key,
        )

    def _on_cluster_updated(
        self,
        cache_name: str,
        cluster_key: wifi.WiFiFrameKey,
        sensor_cluster: SensorCluster,
    ) -> bool:
        if not isinstance(sensor_cluster, csi_cluster.CSICluster):
            raise TypeError(f"CSIPool received unexpected cluster type: {type(sensor_cluster).__name__}")

        if cache_name == _CACHE_CALIBRATION:
            if self._emit_calibration_csi:
                self._try_callbacks(sensor_cluster)
            return False

        identity = sensor_cluster.frame_identity
        if isinstance(identity, csi_association.FrameIdentity) and identity.timestamp_required:
            self._try_callbacks(sensor_cluster)
            # A partial callback must not split a timestamp-associated
            # transmission into singleton clusters by removing it early.
            return sensor_cluster.is_complete

        all_callbacks_fired = self._try_callbacks(sensor_cluster)
        return all_callbacks_fired and np.any(sensor_cluster.completion)

    def _get_cluster_cache_timeout(self, cache_name: str) -> float | None:
        return self._ota_cache_timeout if cache_name == _CACHE_OTA else None
