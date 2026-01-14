#!/usr/bin/env python3

from weakref import WeakKeyDictionary
from collections import OrderedDict
from typing import Callable
import numpy as np
import threading
import binascii
import logging
import time
import json

from . import calibration
from . import constants
from . import cluster
from . import board
from . import util
from . import csi

class _CSICallback(object):
    def __init__(self, cb: Callable[[cluster.CSICluster], None], cb_predicate: Callable[[np.ndarray, float], bool] = None):
        # By default, provide csi if CSI is available from all antennas
        self.cb_predicate = cb_predicate
        self.cb = cb

        # Track fired state per cluster.CSICluster object
        self.fired = WeakKeyDictionary()

    def try_call(self, csi_cluster: cluster.CSICluster):
        # Check if callback has already been fired for this cluster.CSICluster object
        if self.fired.get(csi_cluster, False):
            return True

        # Check if callback needs to be called: Use predicate function if defined, otherwise call if all antennas have CSI
        callback_required = False
        if self.cb_predicate is not None:
            callback_required = self.cb_predicate(csi_cluster.get_completion(), csi_cluster.get_age())
        else:
            callback_required = csi_cluster.get_completion_all()

        if callback_required:
            self.cb(csi_cluster)

            # Mark as fired for this cluster.CSICluster object
            self.fired[csi_cluster] = True
            return True

        return False

class Pool(object):
    """
        A Pool is a collection of ESPARGOS boards.
        The pool manages the clustering of CSI data from multiple ESPARGOS sensors (antennas)
        that belong to the same WiFi packet and provides :class:'cluster.CSICluster' objects to registered callbacks.
    """
    def __init__(self, boards: list[board.Board], ota_cache_timeout=5, refgen_boards=None):
        """
        Constructor for the Pool class.

        :param boards: A list of ESPARGOS boards that belong to the pool
        :param ota_cache_timeout: Optional. The timeout in seconds after which over-the-air CSI data is considered stale and discarded
                                  if the cluster is not complete
        :param refgen_boards: Optional. In some multi-board setups, the calibration signal is provided by (a) separate ESPARGOS device(s)
                              that is / are not part of the pool (only controller is used to generate packets, sensors not used).
                              If provided, sends calibration command to these boards, which will then generate the calibration signal
                              during calibration phase.
        """
        self.logger = logging.getLogger("pyespargos.pool")
        self.boards = boards
        self.refgen_boards = refgen_boards if refgen_boards is not None else []

        self.ota_cache_timeout = ota_cache_timeout

        # We have two caches: One for calibration packets, the other one for over-the-air packets
        self.cluster_cache_calib_lock = threading.Lock()
        self.cluster_cache_calib = OrderedDict[str, cluster.CSICluster]()
        self.cluster_cache_ota = OrderedDict[str, cluster.CSICluster]()

        self.input_list = list()
        self.input_cond = threading.Condition()

        for board_num, board in enumerate(self.boards):
            board.add_consumer(self.input_list, self.input_cond, board_num)

        self.callbacks: list[_CSICallback] = []
        self.logger.info(f"Created new pool with {len(boards)} board(s)")

        self.stored_calibration: calibration.CSICalibration = None
        self.stats = dict()

    def _assert_same_across_boards(self, values: list, what: str):
        """
        Ensure all entries in `values` are identical (after canonical JSON encoding for dict/list).
        """
        if not values:
            raise ValueError(f"{what}: no boards in pool")

        def canon(v):
            if isinstance(v, (dict, list)):
                return json.dumps(v, sort_keys=True, separators=(",", ":"))
            return v

        c0 = canon(values[0])
        for i, v in enumerate(values[1:], start=1):
            if canon(v) != c0:
                raise ValueError(f"{what}: mismatch between boards (board 0 != board {i})")

    def _assert_same_dict_across_boards(self, dicts: list[dict], what: str, ignore_keys: set[str] | None = None):
        """
        Ensure all dicts are identical, optionally ignoring specific keys.
        """
        if not dicts:
            raise ValueError(f"{what}: no boards in pool")
        ignore_keys = ignore_keys or set()

        def strip_ignored(d: dict):
            return {k: v for k, v in d.items() if k not in ignore_keys}

        stripped = [strip_ignored(d) for d in dicts]
        self._assert_same_across_boards(stripped, what)

    def set_rfswitch(self, state: csi.rfswitch_state_t):
        """
        Set RF switch state for all boards in the pool.

        :param state: The RF switch state to set, must be one of :class:`csi.rfswitch_state_t`
        """
        for board in self.boards + self.refgen_boards:
            board.set_rfswitch(state)

    def get_rfswitch(self) -> csi.rfswitch_state_t:
        """
        Get RF switch state from the first board in the pool.

        :return: The RF switch state of the first board in the pool
        """
        if not self.boards:
            raise ValueError("No boards in pool to get RF switch state from")
        
        states = [b.get_rfswitch() for b in self.boards]
        self._assert_same_across_boards(states, "RF switch state")
        return states[0]

    def set_mac_filter(self, mac_filter: dict):
        """
        Set the MAC address filter for all boards in the pool. Will only accept packets from the specified MAC address.

        This is forwarded to :meth:`pyespargos.board.Board.set_mac_filter` for each board.
        """
        for board in self.boards:
            board.set_mac_filter(mac_filter)

    def clear_mac_filter(self):
        """
        Clear the MAC address filter for all boards in the pool.
        """
        for board in self.boards:
            board.clear_mac_filter()

    def get_mac_filter(self) -> dict:
        """
        Return MAC filter configuration; sanity-check all boards report the same value.

        This is forwarded to :meth:`pyespargos.board.Board.get_mac_filter` for each board.
        """
        filters = [b.get_mac_filter() for b in self.boards]
        self._assert_same_across_boards(filters, "MAC filter")
        return filters[0]


    def get_csi_acquire_config(self) -> dict:
        """
        Return CSI acquire config; sanity-check all boards report the same value.
        """
        cfgs = [b.get_csi_acquire_config() for b in self.boards]
        self._assert_same_across_boards(cfgs, "CSI acquire config")
        return cfgs[0]

    def set_csi_acquire_config(self, config: dict):
        """
        Set CSI acquisition configuration on all boards in this pool and sanity-check that all boards
        end up with the same config.

        This is forwarded to :meth:`pyespargos.board.Board.set_csi_acquire_config` for each board.
        For the expected JSON/dict format, refer to that method's documentation.

        :param config: CSI acquisition configuration dict to apply to all boards.
        :raises ValueError: If boards in the pool disagree on the resulting config after applying.
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        for b in self.boards:
            b.set_csi_acquire_config(config)
        _ = self.get_csi_acquire_config()

    def get_gain_settings(self) -> dict:
        """
        Return gain settings; sanity-check all boards report the same value.
        """
        settings = [b.get_gain_settings() for b in self.boards]
        self._assert_same_across_boards(settings, "Gain settings")
        return settings[0]

    def set_gain_settings(self, settings: dict):
        """
        Set gain settings on all boards in this pool and sanity-check that all boards end up with the same settings.

        This is forwarded to :meth:`pyespargos.board.Board.set_gain_settings` for each board.
        For the expected JSON/dict format, refer to that method's documentation.

        :param settings: Gain settings dict to apply to all boards.
        :raises ValueError: If boards in the pool disagree on the resulting settings after applying.
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        for b in self.boards:
            b.set_gain_settings(settings)
        _ = self.get_gain_settings()

    def get_wificonf(self) -> dict:
        """
        Return WiFi config; sanity-check boards report the same value.

        Consistency check ignores "calib-source" and "calib-mode" (they may legitimately differ).
        """
        wificonfs = [b.get_wificonf() for b in self.boards]
        self._assert_same_dict_across_boards(
            wificonfs,
            "WiFi config",
            ignore_keys={"calib-source", "calib-mode"},
        )
        return wificonfs[0]

    def set_wificonf(self, wificonf: dict):
        """
        Set WiFi config on all boards and sanity-check resulting configs match across boards.

        This is forwarded to :meth:`pyespargos.board.Board.set_wificonf` for each board.
        For the expected JSON/dict format, refer to that method's documentation.

        Consistency check ignores "calib-source" and "calib-mode" (they may legitimately differ).

        :param wificonf: WiFi configuration dict to apply to all boards.
        :raises ValueError: If boards in the pool disagree on the resulting config after applying (excluding ignored keys).
        :raises EspargosUnexpectedResponseError: If any board returns an unexpected response.
        """
        for b in self.boards:
            b.set_wificonf(wificonf)
        _ = self.get_wificonf()

    def start(self):
        """
        Start the streaming of CSI data for all boards in the pool.
        """
        for board in self.boards:
            board.start()

    def stop(self):
        """
        Stop the streaming of CSI data for all boards in the pool.
        """
        for board in self.boards:
            board.stop()

    def add_csi_callback(self, cb: Callable[[cluster.CSICluster], None], cb_predicate: Callable[[np.ndarray, float], bool] = None):
        """
        Register callback function that is invoked whenever a new CSI cluster is completed.

        :param cb: The function to call, gets instance of class :class:`.cluster.CSICluster` as parameter
        :param cb_predicate: A function with signature :code:`(csi_completion_state, csi_age)` that defines the conditions under which
            clustered CSI is regarded as completed and thus provided to the callback.
            :code:`csi_completion_state` is a tensor of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)`,
            and :code:`csi_age` is the age of the packet (relative to when any sensor first received it) in seconds
            If :code:`cb_predicate` returns true, clustered CSI is regarded as completed.
            If no predicate is provided, the default behavior is to trigger the callback when CSI has been received
            from all sensors on all boards. If :code:`calibrated` is true (default), callback is provided CSI that is already phase-calibrated.
        """
        self.callbacks.append(_CSICallback(cb, cb_predicate))

    def _clusters_to_calibration(self, board_num = None):
        """
        Convert collected calibration clusters to phase calibration values.

        :param board_num: If provided, only process calibration clusters for the specified board number
        """
        # Take snapshot of current calibration clusters under lock
        with self.cluster_cache_calib_lock:
            clusters = list(self.cluster_cache_calib.values())

        complete_clusters_lltf = []
        complete_clusters_ht20 = []
        complete_clusters_ht40 = []

        channel_primary = None
        channel_secondary = None

        any_csi_count = 0
        for cluster in clusters:
            if channel_primary is None:
                channel_primary = cluster.get_primary_channel()
                channel_secondary = cluster.get_secondary_channel()
            else:
                assert(channel_primary == cluster.get_primary_channel())
                assert(channel_secondary == cluster.get_secondary_channel())

            completion = cluster.get_completion()[board_num] if board_num is not None else cluster.get_completion()
            if np.any(completion):
                any_csi_count = any_csi_count + 1

            if np.all(completion):
                if cluster.has_lltf():
                    complete_clusters_lltf.append(cluster.deserialize_csi_lltf()[board_num] if board_num is not None else cluster.deserialize_csi_lltf())
                if cluster.has_ht20ltf():
                    complete_clusters_ht20.append(cluster.deserialize_csi_ht20ltf()[board_num] if board_num is not None else cluster.deserialize_csi_ht20ltf())
                if cluster.has_ht40ltf():
                    complete_clusters_ht40.append(cluster.deserialize_csi_ht40ltf()[board_num] if board_num is not None else cluster.deserialize_csi_ht40ltf())

        if board_num is not None:
            self.logger.info(f"Board {self.boards[board_num].get_name()}: Collected {any_csi_count} calibration clusters:")
        else:
            self.logger.info(f"Collected {any_csi_count} calibration clusters:")
        self.logger.info(f"  - {len(complete_clusters_ht40)} complete clusters with HT40-LTF")
        self.logger.info(f"  - {len(complete_clusters_ht20)} complete clusters with HT20-LTF")
        self.logger.info(f"  - {len(complete_clusters_lltf)} complete clusters with L-LTF")

        if len(complete_clusters_ht20) == 0 and len(complete_clusters_ht40) > 0:
            # If we only have HT40 calibration, we can still proceed: Use corresponding subcarriers from HT40 for HT20 calibration
            self.logger.warning("No HT20 calibration clusters received, deriving HT20 calibration from HT40 calibration")
            complete_clusters_ht20 = [util.extract_ht20_subcarriers_from_ht40(csi_ht40, cluster.get_secondary_channel_relative()) for csi_ht40 in complete_clusters_ht40]
        elif len(complete_clusters_ht20) > 0:
            util.remove_mean_sto(np.asarray(complete_clusters_ht20))

        # Deriving L-LTF CSI from HT20 CSI is not supported, there appears to be an unknown phase offset between the two...?
        if any_csi_count < 5:
            raise Exception("ESPARGOS calibration failed, did not receive enough calibration clusters.")

        return complete_clusters_lltf, complete_clusters_ht20, complete_clusters_ht40, channel_primary, channel_secondary

    def calibrate(self, per_board = True, duration = 2, exithandler = None, cable_lengths = None, cable_velocity_factors = None):
        """
        Run calibration for a specified duration.

        :param per_board: True to calibrate each board separately, False to calibrate all boards together.
                          Set to False if the same phase reference signal is used for all boards, otherwise set to True.
        :param duration: The duration in seconds for which calibration should be run
        :param exithandler: An optional exit handler that can be used to stop calibration prematurely if :code:`exithandler.running` is set to False in a separate thread
        :param cable_lengths: The lengths of the feeder cables that distribute the clock and phase calibration signal to the ESPARGOS boards, in meters.
                              Only needed for phase-coherent multi-board setups, omit if all cables have the same length.
        :param cable_velocity_factors: The velocity factors of the feeder cables that distribute the clock and phase calibration signal to the ESPARGOS boards
                                       Must be the same length as :code:`cable_lengths`, and all entries should be in the range [0, 1].
        """
        # Clear calibration cache
        with self.cluster_cache_calib_lock:
            self.cluster_cache_calib.clear()

        # Back up and clear MAC filter
        previous_mac_filter = self.get_mac_filter()
        self.clear_mac_filter()

        # Enable calibration mode
        self.logger.info("Starting calibration")
        previous_rfswitch_state = self.get_rfswitch()
        self.set_rfswitch(csi.rfswitch_state_t.SENSOR_RFSWITCH_REFERENCE)

        # Run calibration for specified duration
        start = time.time()
        while (time.time() - start < duration) and (exithandler is None or exithandler.running):
            self.run()

        # Disable calibration mode
        self.logger.info("Finished calibration")
        self.set_rfswitch(previous_rfswitch_state)
        self.set_mac_filter(previous_mac_filter)

        # Collect calibration packets and compute calibration phases
        if per_board:
            phase_calibrations_lltf = []
            phase_calibrations_ht20 = []
            phase_calibrations_ht40 = []

            for board_num in range(len(self.boards)):
                complete_clusters_lltf, complete_clusters_ht20, complete_clusters_ht40, channel_primary, channel_secondary = self._clusters_to_calibration(board_num)

                phase_calibrations_lltf.append(util.csi_interp_iterative(np.asarray(complete_clusters_lltf))) if len(complete_clusters_lltf) > 0 else np.full(self.get_shape()[1:] + (csi.LEGACY_COEFFICIENTS_PER_CHANNEL,), np.nan)
                phase_calibrations_ht20.append(util.csi_interp_iterative(np.asarray(complete_clusters_ht20))) if len(complete_clusters_ht20) > 0 else np.full(self.get_shape()[1:] + (csi.HT_COEFFICIENTS_PER_CHANNEL,), np.nan)
                phase_calibrations_ht40.append(util.csi_interp_iterative(np.asarray(complete_clusters_ht40))) if len(complete_clusters_ht40) > 0 else np.full(self.get_shape()[1:] + (csi.HT_COEFFICIENTS_PER_CHANNEL * 2 + csi.HT40_GAP_SUBCARRIERS,), np.nan)

            self.stored_calibration = calibration.CSICalibration(self.boards, channel_primary, channel_secondary, np.asarray(phase_calibrations_lltf), np.asarray(phase_calibrations_ht20), np.asarray(phase_calibrations_ht40))

        else:
            complete_clusters_lltf, complete_clusters_ht20, complete_clusters_ht40, channel_primary, channel_secondary = self._clusters_to_calibration()

            phase_calibrations_lltf = util.csi_interp_iterative(np.asarray(complete_clusters_lltf)) if len(complete_clusters_lltf) > 0 else np.full(self.get_shape() + (csi.LEGACY_COEFFICIENTS_PER_CHANNEL,), np.nan)
            phase_calibrations_ht20 = util.csi_interp_iterative(np.asarray(complete_clusters_ht20)) if len(complete_clusters_ht20) > 0 else np.full(self.get_shape() + (csi.HT_COEFFICIENTS_PER_CHANNEL,), np.nan)
            phase_calibration_ht40 = util.csi_interp_iterative(np.asarray(complete_clusters_ht40)) if len(complete_clusters_ht40) > 0 else np.full(self.get_shape() + (csi.HT_COEFFICIENTS_PER_CHANNEL * 2 + csi.HT40_GAP_SUBCARRIERS,), np.nan)

            self.stored_calibration = calibration.CSICalibration(self.boards, channel_primary, channel_secondary, phase_calibrations_lltf, phase_calibrations_ht20, phase_calibration_ht40, board_cable_lengths=cable_lengths, board_cable_vfs=cable_velocity_factors)

    def get_calibration(self):
        """
        Get the stored calibration values.

        :return: The stored calibration values as a :class:`.calibration.CSICalibration` object
        """
        return self.stored_calibration

    def get_shape(self):
        """
        Get the outer shape of the stored data, i.e., only the antenna dimensions and not subcarrier dimensions or similar.
        """
        return (len(self.boards), constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)

    def get_stats(self):
        """
        Get collected statistics about the pool.
        """
        return self.stats

    def run(self):
        """
        Process incoming CSI data packets and call registered callbacks if CSI clusters are complete.
        Repeatedly call this function from your main loop or from a separate thread.
        May block for a short amount of time if no data is available.
        """
        with self.input_cond:
            self.input_cond.wait(timeout = 0.5)
            packets = [p for p in self.input_list]
            self.input_list.clear()

        self._handle_packets(packets)

    def _handle_packets(self, packets):
        self.stats["packet_backlog"] = len(packets)

        for pkt in packets:
            esp_num, serialized_csi, board_num = pkt[0], pkt[1], pkt[2]

            source_mac_str = binascii.hexlify(bytearray(serialized_csi.source_mac)).decode("utf-8")
            dest_mac_str = binascii.hexlify(bytearray(serialized_csi.dest_mac)).decode("utf-8")

            # Identifier (here: MAC address & sequence control number)
            cluster_id = f"{source_mac_str}-{dest_mac_str}-{serialized_csi.seq_ctrl.seg:03x}-{serialized_csi.seq_ctrl.frag:01x}"

            # Prepare a cache entry for a new cluster with a different and add received data to the current cluster
            if serialized_csi.is_calib:
                with self.cluster_cache_calib_lock:
                    if cluster_id not in self.cluster_cache_calib:
                        self.cluster_cache_calib[cluster_id] = cluster.CSICluster(source_mac_str, dest_mac_str, serialized_csi.seq_ctrl, [b.revision for b in self.boards])

                    self.cluster_cache_calib[cluster_id].add_csi(board_num, esp_num, serialized_csi)
            else:
                if cluster_id not in self.cluster_cache_ota:
                    self.cluster_cache_ota[cluster_id] = cluster.CSICluster(source_mac_str, dest_mac_str, serialized_csi.seq_ctrl, [b.revision for b in self.boards])

                # Add received data for the antenna to the current cluster
                self.cluster_cache_ota[cluster_id].add_csi(board_num, esp_num, serialized_csi)

        # Check OTA cluster cache for packets where callback is due and for stale packets
        stale = set()
        for id in self.cluster_cache_ota.keys():
            all_callbacks_fired = True
            for cb in self.callbacks:
                all_callbacks_fired = all_callbacks_fired and cb.try_call(self.cluster_cache_ota[id])

            if all_callbacks_fired:
                stale.add(id)

        for id in self.cluster_cache_ota.keys():
            if self.cluster_cache_ota[id].get_age() > self.ota_cache_timeout:
                stale.add(id)

        for id in stale:
            del self.cluster_cache_ota[id]