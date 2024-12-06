#!/usr/bin/env python3

from collections import OrderedDict
import numpy as np
import threading
import binascii
import logging
import ctypes
import time

from espargos import board

from . import constants
from . import util
from . import csi

class ClusteredCSI(object):
    """
        A ClusteredCSI object represents a collection of CSI data estimated for the same WiFi packet.

        The class clusters the CSI data from multiple ESPARGOS sensors (antennas), which may belong to the same or different ESPARGOS boards.
        It is used to store CSI data until it is complete and can be provided to a callback.
        CSI data may be from calibration packets or over-the-air packets.
    """
    def __init__(self, source_mac, dest_mac, seq_ctrl, boardcount):
        """
        Constructor for the ClusteredCSI class.

        All channel coefficients added to this class belong to the same WiFi packet,
        so they share the same source and destination MAC addresses and sequence control field.
        The constructor pre-allocates memory for the CSI data.

        :param source_mac: The source MAC address of the WiFi packet
        :param dest_mac: The destination MAC address of the WiFi packet
        :param seq_ctrl: The sequence control field of the WiFi packet
        :param boardcount: The number of ESPARGOS boards in the pool
        """
        self.source_mac = source_mac
        self.dest_mac = dest_mac
        self.seq_ctrl = seq_ctrl

        self.timestamp = time.time()
        self.boardcount = boardcount
        self.serialized_csi_all = [[[None for c in range(constants.ANTENNAS_PER_ROW)] for r in range(constants.ROWS_PER_BOARD)] for b in range(self.boardcount)]
        self.shape = (self.boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)

        # Remember which sensors have already provided CSI data
        self.csi_completion_state = np.full(self.shape, False)
        self.csi_completion_state_all = False

        # Allocate memory for the channel coefficients and build views for the different parts of them
        self.complex_csi_all = np.full(self.shape + (ctypes.sizeof(csi.csi_buf_t) // 2, ), fill_value = np.nan, dtype = np.complex64)
        self.complex_csi_lltf = self.complex_csi_all[:,:,:,csi.csi_buf_t.lltf.offset // 2:(csi.csi_buf_t.lltf.offset + csi.csi_buf_t.lltf.size) // 2].view()
        self.complex_csi_htltf_higher = self.complex_csi_all[:,:,:,csi.csi_buf_t.htltf_higher.offset // 2:(csi.csi_buf_t.htltf_higher.offset + csi.csi_buf_t.htltf_higher.size) // 2].view()
        self.complex_csi_htltf_lower = self.complex_csi_all[:,:,:,csi.csi_buf_t.htltf_lower.offset // 2:(csi.csi_buf_t.htltf_lower.offset + csi.csi_buf_t.htltf_lower.size) // 2].view()

        # Allocate memory for the RSSI values
        self.rssi_all = np.full(self.shape, fill_value = np.nan, dtype = np.float32)

    def add_csi(self, board_num, esp_num, serialized_csi, csi_cplx):
        """
        Add CSI data to the cluster.

        :param board_num: The number of the ESPARGOS board that received the CSI data
        :param esp_num: The number of the ESPARGOS sensor within that board that received the CSI data
        :param serialized_csi: The serialized CSI data
        :param csi_cplx: The complex-valued CSI data
        """
        assert(binascii.hexlify(bytearray(serialized_csi.source_mac)).decode("utf-8") == self.source_mac)
        assert(binascii.hexlify(bytearray(serialized_csi.dest_mac)).decode("utf-8") == self.dest_mac)
        assert(serialized_csi.seq_ctrl.seg == self.seq_ctrl.seg)
        assert(serialized_csi.seq_ctrl.frag == self.seq_ctrl.frag)

        # Compute row and column indices from ESPARGOS sensor number
        row = 1 - esp_num // 4
        column = 3 - esp_num % 4

        # Store CSI data to pre-allocated memory
        self.serialized_csi_all[board_num][row][column] = serialized_csi
        self.complex_csi_all[board_num, row, column] = csi_cplx
        self.csi_completion_state[board_num, row, column] = True
        self.csi_completion_state_all = np.all(self.csi_completion_state)
        self.rssi_all[board_num, row, column] = csi.wifi_pkt_rx_ctrl_t(serialized_csi.rx_ctrl).rssi

    def deserialize_csi_lltf(self):
        """
        Deserialize the L-LTF part of the CSI data.

        :return: The L-LTF part of the CSI data as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi.csi_buf_t.lltf.size // 2)`
        """
        return self.complex_csi_lltf

    def deserialize_csi_ht40(self):
        """
        Deserialize the HT-LTF part of the CSI data.

        :return: The HT-LTF part of the CSI data as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, (csi.csi_buf_t.htltf_lower.size + csi.HT40_GAP_SUBCARRIERS * 2 + csi.csi_buf_t.htltf_higher.size) // 2)`
        """
        assert(self.is_ht40())
        loc = self.get_secondary_channel_relative()
        assert(loc != 0)

        csi_ht40 = np.zeros(self.shape + ((csi.csi_buf_t.htltf_lower.size + csi.HT40_GAP_SUBCARRIERS * 2 + csi.csi_buf_t.htltf_higher.size) // 2,), dtype = np.complex64)
        csi_higher = csi_ht40[:,:,:,:csi.csi_buf_t.htltf_lower.size // 2].view()
        csi_lower = csi_ht40[:,:,:,-csi.csi_buf_t.htltf_higher.size // 2:].view()
        csi_higher[:] = self.complex_csi_htltf_lower
        csi_lower[:] = self.complex_csi_htltf_higher

        # Secondary channel experiences phase shift by pi / 2
        # This is likely due to the pi / 2 phase shift specified for the pilot symbols,
        # see IEEE 80211-2012 section 20.3.9.3.4 L-LTF definition
        if loc == 1:
            csi_higher = csi_higher * np.exp(-1.0j * np.pi / 2)
        else:
            csi_lower = csi_lower * np.exp(-1.0j * np.pi / 2)

        return csi_ht40

    def is_ht40(self):
        """
        Check if the packet is a HT40 packet, i.e., if it uses channel bonding and hence occupies two 20 MHz channels.
        """
        return csi.wifi_pkt_rx_ctrl_t(self._first_complete_sensor().rx_ctrl).cwb == 1

    def get_secondary_channel_relative(self):
        """
        Get the relative position of the secondary channel with respect to the primary channel.

        :return: 0 if no secondary channel is used, 1 if the secondary channel is above the primary channel, -1 if the secondary channel is below the primary channel
        """
        match csi.wifi_pkt_rx_ctrl_t(self._first_complete_sensor().rx_ctrl).secondary_channel:
            case 0:
                return 0
            case 1:
                return 1
            case 2:
                return -1

    def get_primary_channel(self):
        """
        Get the primary channel number.

        :return: The primary channel number
        """
        return csi.wifi_pkt_rx_ctrl_t(self._first_complete_sensor().rx_ctrl).channel

    def get_secondary_channel(self):
        """
        Get the secondary channel number.

        :return: The secondary channel number
        """
        return self.get_primary_channel() + 4 * self.get_secondary_channel_relative()

    def get_completion(self):
        """
        Get the completion state of the CSI data.

        :return: A boolean numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)` that indicates which sensors have provided CSI data
        """
        return self.csi_completion_state

    def get_completion_all(self):
        """
        Get the global completion state of the CSI data, i.e., whether all sensors have provided CSI data.

        :return: True if all sensors have provided CSI data, False otherwise
        """
        return self.csi_completion_state_all

    def get_age(self):
        """
        Get the age of the CSI data, in seconds.

        The age is only approximate, it is based on the timestamp when the :class:`.ClusteredCSI` object was created,
        not on the sensor timestamps.

        :return: The age of the CSI data, in seconds
        """
        return time.time() - self.timestamp

    def get_sensor_timestamps(self):
        """
        Get the (nanosecond-precision) timestamps at which the WiFi packet was received by the sensors.
        This timestamp *includes* the offset that the chip derived from the CSI.

        :return: A numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)` that contains the sensor timestamps in seconds
        """
        sensor_timestamps = np.full(self.shape, np.nan, dtype = np.float128)

        def append_sensor_timestamp(b, r, a, serialized_csi):
            timestamp_ns = np.float128(self._nanosecond_timestamp(serialized_csi))
            sensor_timestamps[b, r, a] = np.float128(timestamp_ns) / 1e9

        self._foreach_complete_sensor(append_sensor_timestamp)
        return sensor_timestamps

    def get_host_timestamp(self):
        """
        Get the timestamp at which the :class:`.ClusteredCSI` object was created, which is approximately when the first sensor received the CSI data.

        :return: The timestamp at which the first sensor received the CSI data, in seconds since the epoch
        """
        return self.timestamp

    def get_rssi(self):
        """
        Get the RSSI values of the WiFi packet.
        """
        return self.rssi_all

    def get_source_mac(self):
        """
        Get the source MAC address of the WiFi packet.

        :return: The source MAC address of the WiFi packet
        """
        return self.source_mac

    def get_seq_ctrl(self):
        """
        Get the sequence control field of the WiFi packet.

        :return: The sequence control field of the WiFi packet
        """
        return self.seq_ctrl

    # Internal helper functions
    def _foreach_complete_sensor(self, cb):
        for b, board in enumerate(self.serialized_csi_all):
            for r, row in enumerate(board):
                for a, serialized_csi in enumerate(row):
                    if serialized_csi is not None:
                        cb(b, r, a, serialized_csi)

    def _first_complete_sensor(self):
        for board in self.serialized_csi_all:
            for row in board:
                for serialized_csi in row:
                    if serialized_csi is not None:
                        return serialized_csi

        return None
    
    def _nanosecond_timestamp(self, serialized_csi):
        rxstart_time_cyc = csi.wifi_pkt_rx_ctrl_t(serialized_csi.rx_ctrl).rxstart_time_cyc
        rxstart_time_cyc_dec = csi.wifi_pkt_rx_ctrl_t(serialized_csi.rx_ctrl).rxstart_time_cyc_dec
        rxstart_time_cyc_dec = 2048 - rxstart_time_cyc_dec if rxstart_time_cyc_dec >= 1024 else rxstart_time_cyc_dec
        hw_latched_timestamp_ns = serialized_csi.timestamp * 1000

        # "official" formula by Espressif:
        #timestamp_ns = np.float128(serialized_csi.timestamp * 1000 + ((rxstart_time_cyc * 12500) // 1000) + ((rxstart_time_cyc_dec * 1562) // 1000) - 20800)
        # Formula that is probably more accurate:
        CYC_PERIOD_NS = 1/80e6*1e9
        CYC_DEC_PERIOD_NS = 1/640e6*1e9
        HW_TIMESTAMP_LAG_NS = 20800
        return hw_latched_timestamp_ns - HW_TIMESTAMP_LAG_NS + rxstart_time_cyc * CYC_PERIOD_NS + rxstart_time_cyc_dec * CYC_DEC_PERIOD_NS

class CSICalibration(object):
    def __init__(self, channel_primary, channel_secondary, calibration_values_ht40, timestamp_calibration_values, board_cable_lengths = None, board_cable_vfs = None):
        """
        Constructor for the CSICalibration class.

        This class takes care of storing and applying the phase calibration values for the CSI data as well as calibrating phases.
        It also supports multi-board setups with different lengths for the cables that distribute the clock and phase calibration signal.

        :param channel_primary: The primary channel number
        :param channel_secondary: The secondary channel number
        :param calibration_values_ht40: The phase calibration values for the HT40 channel, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, (csi.csi_buf_t.htltf_lower.size + csi.HT40_GAP_SUBCARRIERS * 2 + csi.csi_buf_t.htltf_higher.size) // 2)`
        :param timestamp_calibration_values: The reception timestamp offset calibration values, as a numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)`
        :param board_cable_lengths: The lengths of the cables that distribute the clock and phase calibration signal to the ESP32 boards, in meters
        :param board_cable_vfs: The velocity factors of the cables that distribute the clock and phase calibration signal to the ESP32 boards
        """
        self.channel_primary = channel_primary
        self.channel_secondary = channel_secondary
        self.frequencies_ht40 = util.get_frequencies_ht40(channel_primary, channel_secondary)
        wavelengths = util.get_calib_trace_wavelength(self.frequencies_ht40).astype(calibration_values_ht40.dtype)
        tracelengths = np.asarray(constants.CALIB_TRACE_LENGTH, dtype = calibration_values_ht40.dtype)# - np.asarray(constants.CALIB_TRACE_EMPIRICAL_ERROR)
        prop_calib_each_board = np.exp(-1.0j * 2 * np.pi * tracelengths[:,:,np.newaxis] / wavelengths[np.newaxis, np.newaxis])
        prop_delay_each_board = np.asarray(constants.CALIB_TRACE_LENGTH) / np.asarray(constants.CALIB_TRACE_GROUP_VELOCITY)

        # TODO: Account for board-specific time offsets in timestamp_calibration_values

        # Account for additional board-specific phase offsets due to different feeder cable lengths in a multi-board antenna array system
        if board_cable_lengths is not None:
            assert(board_cable_vfs is not None)
            board_cable_lengths = np.asarray(board_cable_lengths)
            board_cable_vfs = np.asarray(board_cable_vfs)
            subcarrier_cable_wavelengths = util.get_cable_wavelength(util.get_frequencies_ht40(channel_primary, channel_secondary), board_cable_vfs).astype(calibration_values_ht40.dtype)
            board_phase_offsets = np.exp(-1.0j * 2 * np.pi * board_cable_lengths[:,np.newaxis] / subcarrier_cable_wavelengths)
            prop_calib = np.einsum("bs,ras->bras", board_phase_offsets, prop_calib_each_board)
            coeffs_without_propdelay = np.einsum("bras,bras->bras", calibration_values_ht40, np.conj(prop_calib))
        else:
            coeffs_without_propdelay = np.einsum("bras,ras->bras", calibration_values_ht40, np.conj(prop_calib_each_board))

        self.calibration_values_ht40 = np.exp(-1.0j * np.angle(coeffs_without_propdelay))
        self.calibration_values_ht40_flat = np.sum(np.exp(-1.0j * np.angle(coeffs_without_propdelay)), axis = -1)
        self.timestamp_calibration_values = timestamp_calibration_values - prop_delay_each_board[np.newaxis,:,:]

    def apply_ht40(self, values, sensor_timestamps):
        """
        Apply phase calibration to the provided HT40 CSI data.
        Also accounts for subcarrier-specific phase offsets, e.g., due to low-pass filter characteristic of baseband signal path inside the ESP32,
        but can be less accurate if reference channel is not frequency-flat.

        :param values: The CSI data to which the phase calibration should be applied, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, (csi.csi_buf_t.htltf_lower.size + csi.HT40_GAP_SUBCARRIERS * 2 + csi.csi_buf_t.htltf_higher.size) // 2)`
        :param sampling_times: The precise time when the CSI data was sampled, as a numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)`
        :return: The phase-calibrated and time offset-calibrated CSI data
        """
        # TODO: Check if primary and secondary channel match

        delay = sensor_timestamps - self.timestamp_calibration_values
        delay = delay - np.mean(delay)

        subcarrier_range = np.arange(-values.shape[-1] // 2, values.shape[-1] // 2)[np.newaxis,np.newaxis,np.newaxis,:]
        sto_delay_correction = np.exp(-1.0j * 2 * np.pi * delay[:,:,:,np.newaxis] * constants.WIFI_SUBCARRIER_SPACING * subcarrier_range)

        csi = np.einsum("bras,bras,bras->bras", values, sto_delay_correction, self.calibration_values_ht40)

        # Mean delay should be zero
        mean_sto = np.angle(np.sum(csi[...,1:] * np.conj(csi[...,:-1]))) / (2 * np.pi)
        mean_sto_correction = np.exp(-1.0j * 2 * np.pi * mean_sto * np.arange(-csi.shape[-1] // 2, csi.shape[-1] // 2))
        return csi * mean_sto_correction[np.newaxis, np.newaxis, np.newaxis, :]
        
    def apply_l20(self, values, sensor_timestamps):
        """
        Apply phase calibration to the provided L20 CSI data.
        Also accounts for subcarrier-specific phase offsets, e.g., due to low-pass filter characteristic of baseband signal path inside the ESP32,
        but can be less accurate if reference channel is not frequency-flat.

        :param values: The CSI data to which the phase calibration should be applied, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, (csi.csi_buf_t.htltf_lower.size + csi.HT40_GAP_SUBCARRIERS * 2 + csi.csi_buf_t.htltf_higher.size) // 2)`
        :param sampling_times: The precise time when the CSI data was sampled, as a numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)`
        :return: The phase-calibrated and time offset-calibrated CSI data
        """
        # TODO: Check if primary and secondary channel match

        delay = sensor_timestamps - self.timestamp_calibration_values
        delay = delay - np.mean(delay)

        subcarrier_range = np.arange(-values.shape[-1] // 2, values.shape[-1] // 2)[np.newaxis,np.newaxis,np.newaxis,:]
        sto_delay_correction = np.exp(-1.0j * 2 * np.pi * delay[:,:,:,np.newaxis] * constants.WIFI_SUBCARRIER_SPACING * subcarrier_range)

        csi_vec = np.einsum("bras,bras,bras->bras", values, sto_delay_correction, self.calibration_values_ht40[:,:,:,:(csi.csi_buf_t.lltf.size//2)])

        # Mean delay should be zero
        mean_sto = np.angle(np.sum(csi_vec[...,1:] * np.conj(csi_vec[...,:-1]))) / (2 * np.pi)
        mean_sto_correction = np.exp(-1.0j * 2 * np.pi * mean_sto * np.arange(-csi_vec.shape[-1] // 2, csi_vec.shape[-1] // 2))
        return csi_vec * mean_sto_correction[np.newaxis, np.newaxis, np.newaxis, :]

    def apply_ht40_flat(self, values):
        """
        Apply phase calibration to the provided HT40 CSI data.
        Assume constant phase offset over all subcarriers, i.e., ignore effects like low-pass characteristic.

        :param values: The CSI data to which the phase calibration should be applied, as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, (csi.csi_buf_t.htltf_lower.size + csi.HT40_GAP_SUBCARRIERS * 2 + csi.csi_buf_t.htltf_higher.size) // 2)`
        :return: The phase-calibrated CSI data
        """
        # TODO: Check if primary and secondary channel match
        return np.einsum("bras,bra->bras", values, self.calibration_values_ht40_flat)

    def apply_timestamps(self, timestamps):
        """
        Apply time offset calibration to the provided timestamps.

        :param timestamps: The timestamps to which the calibration should be applied, as a numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)`
        :return: The calibrated timestamps
        """
        return timestamps - self.timestamp_calibration_values

class _CSICallback(object):
    def __init__(self, cb, cb_predicate = None):
        # By default, provide csi if CSI is available from all antennas
        self.cb_predicate = cb_predicate
        self.cb = cb
        self.fired = False

    def try_call(self, csi_cluster):
        # Already fired, no need to call callback again
        if self.fired:
            return True

        # Check if callback needs to be called: Use predicate function if defined, otherwise call if all antennas have CSI
        callback_required = False
        if self.cb_predicate is not None:
            callback_required = self.cb_predicate(csi_cluster.get_completion(), csi_cluster.get_age())
        else:
            callback_required = csi_cluster.get_completion_all()

        if callback_required:
            self.cb(csi_cluster)
            return True

        return False

class Pool(object):
    """
        A Pool is a collection of ESPARGOS boards.
        The pool manages the clustering of CSI data from multiple ESPARGOS sensors (antennas)
        that belong to the same WiFi packet and provides :class:'ClusteredCSI' objects to registered callbacks.
    """
    def __init__(self, boards, ota_cache_timeout=5):
        """
        Constructor for the Pool class.

        :param boards: A list of ESPARGOS boards that belong to the pool
        :param ota_cache_timeout: The timeout in seconds after which over-the-air CSI data is considered stale and discarded
                                  if the cluster is not complete
        """
        self.logger = logging.getLogger("pyespargos.pool")
        self.boards = boards

        self.ota_cache_timeout = ota_cache_timeout

        # We have two caches: One for calibration packets, the other one for over-the-air packets
        self.cluster_cache_calib = OrderedDict()
        self.cluster_cache_ota = OrderedDict()

        self.input_list = list()
        self.input_cond = threading.Condition()

        for board_num, board in enumerate(self.boards):
            board.add_consumer(self.input_list, self.input_cond, board_num)

        self.callbacks = []
        self.logger.info(f"Created new pool with {len(boards)} board(s)")

        self.stored_calibration = None
        self.stats = dict()

    def set_calib(self, calibrate):
        """
        Set calibration mode for all boards in the pool.

        :param calibrate: True to enable calibration mode, False to disable it
        """
        for board in self.boards:
            board.set_calib(calibrate)

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

    def add_csi_callback(self, cb, cb_predicate = None):
        """
        Register callback function that is invoked whenever a new CSI cluster is completed.

        :param cb: The function to call, gets instance of class :class:`.ClusteredCSI` as parameter
        :param cb_predicate: A function with signature :code:`(csi_completion_state, csi_age)` that defines the conditions under which
            clustered CSI is regarded as completed and thus provided to the callback.
            :code:`csi_completion_state` is a tensor of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)`,
            and :code:`csi_age` is the age of the packet (relative to when any sensor first received it) in seconds
            If :code:`cb_predicate` returns true, clusterd CSI is regarded as completed.
            If no predicate is provided, the default behavior is to trigger the callback when CSI has been received
            from all sensors on all boards. If :code:`calibrated` is true (default), callback is provided CSI that is already phase-calibrated.
        """
        self.callbacks.append(_CSICallback(cb, cb_predicate))

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
        self.cluster_cache_calib.clear()

        # Enable calibration mode
        self.logger.info("Starting calibration")
        self.set_calib(True)

        # Run calibration for specified duration
        start = time.time()
        while (time.time() - start < duration) and (exithandler is None or exithandler.running):
            self.run()

        # Disable calibration mode
        self.logger.info("Finished calibration")
        self.set_calib(False)

        channel_primary = None
        channel_secondary = None

        # Collect calibration packets and compute calibration phases
        if per_board:
            phase_calibrations = []
            timestamp_calibrations = []

            for board_num, board in enumerate(self.boards):
                complete_clusters = []
                timestamp_offsets = []

                any_csi_count = 0
                for cluster in self.cluster_cache_calib.values():
                    if channel_primary is None:
                        channel_primary = cluster.get_primary_channel()
                        channel_secondary = cluster.get_secondary_channel()
                    else:
                        assert(channel_primary == cluster.get_primary_channel())
                        assert(channel_secondary == cluster.get_secondary_channel())

                    completion = cluster.get_completion()[board_num]
                    if np.any(completion):
                        any_csi_count = any_csi_count + 1

                    if np.all(completion):
                        complete_clusters.append(cluster.deserialize_csi_ht40()[board_num])
                        timestamp_offsets.append(cluster.get_sensor_timestamps()[board_num] - cluster.get_host_timestamp())

                self.logger.info(f"Board {board.get_name()}: Collected {any_csi_count} calibration clusters, out of which {len(complete_clusters)} are complete")
                if len(complete_clusters) == 0:
                    raise Exception("ESPARGOS calibration failed, did not receive phase reference signal")
                phase_calibrations.append(util.csi_interp_iterative(np.asarray(complete_clusters)))
                timestamp_calibrations.append(np.mean(np.asarray(timestamp_offsets), axis = 0))

            self.stored_calibration = CSICalibration(channel_primary, channel_secondary, np.asarray(phase_calibrations), np.asarray(timestamp_calibrations))

        else:
            complete_clusters = []
            timestamp_offsets = []

            for cluster in self.cluster_cache_calib.values():
                if channel_primary is None:
                    channel_primary = cluster.get_primary_channel()
                    channel_secondary = cluster.get_secondary_channel()
                else:
                    assert(channel_primary == cluster.get_primary_channel())
                    assert(channel_secondary == cluster.get_secondary_channel())

                completion = cluster.get_completion()
                if np.all(completion):
                    complete_clusters.append(cluster.deserialize_csi_ht40())
                    timestamp_offsets.append(cluster.get_sensor_timestamps() - cluster.get_host_timestamp())

            self.logger.info(f"Pool: Collected {len(self.cluster_cache_calib)} calibration clusters, out of which {len(complete_clusters)} are complete")
            phase_calibration = util.csi_interp_iterative(np.asarray(complete_clusters))
            time_calibration = np.mean(np.asarray(timestamp_offsets), axis = 0)

            self.stored_calibration = CSICalibration(channel_primary, channel_secondary, phase_calibration, time_calibration, board_cable_lengths=cable_lengths, board_cable_vfs=cable_velocity_factors)

    def get_calibration(self):
        """
        Get the stored calibration values.

        :return: The stored calibration values as a :class:`.CSICalibration` object
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

        # Deserialize CSI of all packets
        csi_bufs_int8 = np.zeros((len(packets), ctypes.sizeof(csi.csi_buf_t)), dtype = np.int8)
        for i, pkt in enumerate(packets):
            esp_num, serialized_csi, board_num = pkt[0], pkt[1], pkt[2]
            csi_bufs_int8[i] = serialized_csi.buf

        # The ESP32 provides CSI as int8_t values in (im, re) paris (in this order!)
        # To go from the (re, im) interpretation to (im, re), compute conjugate and multiply by 1.0j.
        csi_bufs_complex = csi_bufs_int8.astype(np.float32).view(np.complex64)
        csi_bufs_complex = -1.0j * np.conj(csi_bufs_complex)

        for pkt, csi_cplx in zip(packets, csi_bufs_complex):
            esp_num, serialized_csi, board_num = pkt[0], pkt[1], pkt[2]

            source_mac_str = binascii.hexlify(bytearray(serialized_csi.source_mac)).decode("utf-8")
            dest_mac_str = binascii.hexlify(bytearray(serialized_csi.dest_mac)).decode("utf-8")

            cluster_cache = self.cluster_cache_calib if serialized_csi.is_calib else self.cluster_cache_ota

            # Prepare a cache entry for a new cluster with a different identifier (here: MAC address & sequence control number)
            cluster_id = f"{source_mac_str}-{dest_mac_str}-{serialized_csi.seq_ctrl.seg:03x}-{serialized_csi.seq_ctrl.frag:01x}"
            if cluster_id not in cluster_cache:
                cluster_cache[cluster_id] = ClusteredCSI(source_mac_str, dest_mac_str, serialized_csi.seq_ctrl, len(self.boards))

            # Add received data for the antenna to the current cluster
            cluster_cache[cluster_id].add_csi(board_num, esp_num, serialized_csi, csi_cplx)

            if not serialized_csi.is_calib:
                # Check cluster cache for packets where callback is due and for stale packets
                stale = set()
                for id in cluster_cache.keys():
                    all_callbacks_fired = True
                    for cb in self.callbacks:
                        all_callbacks_fired = all_callbacks_fired and cb.try_call(cluster_cache[id])

                    if all_callbacks_fired:
                        stale.add(id)

                for id in cluster_cache.keys():
                    if cluster_cache[id].get_age() > self.ota_cache_timeout:
                        stale.add(id)

                for id in stale:
                    del cluster_cache[id]