#!/usr/bin/env python3

import numpy as np
import binascii
import time

from . import revisions
from . import constants
from . import csi


class CSICluster(object):
    """
    A CSICluster object represents a collection of CSI data estimated for the same WiFi packet.

    The class clusters the CSI data from multiple ESPARGOS sensors (antennas), which may belong to the same or different ESPARGOS boards.
    It is used to store CSI data until it is complete and can be provided to a callback.
    CSI data may be from calibration packets or over-the-air packets.
    """

    def __init__(
        self,
        source_mac: str,
        dest_mac: str,
        seq_ctrl: csi.seq_ctrl_t,
        board_revisions: list[revisions.BoardRevision],
    ):
        """
        Constructor for the CSICluster class.

        All channel coefficients added to this class belong to the same WiFi packet,
        so they share the same source and destination MAC addresses and sequence control field.
        The constructor pre-allocates memory for the CSI data.

        :param source_mac: The source MAC address of the WiFi packet
        :param dest_mac: The destination MAC address of the WiFi packet
        :param seq_ctrl: The sequence control field of the WiFi packet
        :param board_revisions: The ESPARGOS board revisions in the pool
        """
        self.source_mac = source_mac
        self.dest_mac = dest_mac
        self.seq_ctrl = seq_ctrl

        self.timestamp = time.time()
        self.board_revisions = board_revisions
        self.serialized_csi_all = [[[None for c in range(constants.ANTENNAS_PER_ROW)] for r in range(constants.ROWS_PER_BOARD)] for b in self.board_revisions]
        self.shape = (
            len(self.board_revisions),
            constants.ROWS_PER_BOARD,
            constants.ANTENNAS_PER_ROW,
        )

        # Remember which sensors have already provided CSI data
        self.csi_completion_state = np.full(self.shape, False)
        self.csi_completion_state_all = False

        # Allocate memory for the RSSI, rf switch state and noise floor values
        self.rssi_all = np.full(self.shape, fill_value=np.nan, dtype=np.float32)
        self.rfswitch_state_all = np.full(self.shape, fill_value=csi.rfswitch_state_t.SENSOR_RFSWITCH_UNKNOWN, dtype=np.uint8)
        self.noise_floor_all = np.full(self.shape, fill_value=np.nan, dtype=np.float32)

    def add_csi(
        self,
        board_num: int,
        esp_num: int,
        serialized_csi: csi.serialized_csi_v1_t | csi.serialized_csi_v3_t,
    ):
        """
        Add CSI data to the cluster.

        :param board_num: The number of the ESPARGOS board that received the CSI data
        :param esp_num: The number of the ESPARGOS sensor within that board that received the CSI data
        :param serialized_csi: The serialized CSI data
        :param csi_cplx: The complex-valued CSI data
        """
        assert binascii.hexlify(bytearray(serialized_csi.source_mac)).decode("utf-8") == self.source_mac
        assert binascii.hexlify(bytearray(serialized_csi.dest_mac)).decode("utf-8") == self.dest_mac
        assert serialized_csi.seq_ctrl.seg == self.seq_ctrl.seg
        assert serialized_csi.seq_ctrl.frag == self.seq_ctrl.frag

        # TODO: Assert that esp_num matches self-identified antenna ID

        # Convert esp_num to row and column, mapping may differ across board revisions
        row, col = self.board_revisions[board_num].esp_num_to_row_col(esp_num)

        # Store CSI data to pre-allocated memory
        self.serialized_csi_all[board_num][row][col] = serialized_csi
        # self.complex_csi_all[board_num, row, col] = csi_cplx # TODO: Will not work for V3 :(
        self.csi_completion_state[board_num, row, col] = True
        self.csi_completion_state_all = np.all(self.csi_completion_state)

        # Handle signed values for RSSI and noise floor (stored as uint32_t in rx_ctrl due to ctypes packing limitations)
        rssi = csi.wifi_pkt_rx_ctrl_v3_t(serialized_csi.rx_ctrl).rssi
        noise_floor = csi.wifi_pkt_rx_ctrl_v3_t(serialized_csi.rx_ctrl).noise_floor
        self.rssi_all[board_num, row, col] = (rssi - 0x100) if (rssi & 0x80) else rssi
        self.noise_floor_all[board_num, row, col] = (noise_floor - 0x100) if (noise_floor & 0x80) else noise_floor
        self.rfswitch_state_all[board_num, row, col] = serialized_csi.rfswitch_state

    def deserialize_csi_lltf(self):
        """
        Deserialize the L-LTF part of the CSI data.

        :return: The L-LTF part of the CSI data as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi.LEGACY_COEFFICIENTS_PER_CHANNEL)`
        """
        assert self.has_lltf()

        csi_lltf = np.zeros(self.shape + (csi.LEGACY_COEFFICIENTS_PER_CHANNEL,), dtype=np.complex64)

        def deserialize_lltf_packet(b, r, a, serialized_csi):
            nonlocal csi_lltf
            csi_lltf_sensor = csi_lltf[b, r, a, :].view()

            # The ESP32 PHY v3 uses the weirdest format for L-LTF CSI.
            # It is provided as 27 subcarriers, each as a 12-bit signed integer stored in a 16-bit container.
            lltf_bytes = np.asarray(csi.csi_buf_v3_lltf_t(serialized_csi.buf).lltf, dtype=np.uint8)

            lo = lltf_bytes[0::2].astype(np.int16)
            hi = lltf_bytes[1::2].astype(np.int16) & 0x0F
            lltf_all = ((hi << 12) >> 4) | lo
            final_re = lltf_all[-1].astype(np.float32)
            lltf_all = lltf_all[:-1]  # Last two bytes of buffer are padding
            lltf_all_cplx = lltf_all.astype(np.float32).view(np.complex64)

            # lltf_all_cplx only contains every second subcarrier, starting from the lowest frequency subcarrier
            # array index = |   0 |   1 |   2 |   3 | ... |     |     |     |  26 |  27 |  28 |  29 | ... |  52 |  50 |   * |   A |
            # subc. index = | -26 |   * | -24 |   * | ... |   * |  -2 |   * |  DC |  *  |   2 |   * | ... |   * |  24 |   * |   * |
            # Numbers = existing subcarriers
            #       * = missing subcarriers that need to be interpolated
            #      DC = DC subcarrier, only exists in forced L-LTF mode, otherwise needs to be interpolated
            #       A = only real part provided if acquire_force_lltf is false, not provided at all if acquire_force_lltf is true
            # Note that the subcarrier with index 26 is *not* measured, so it needs to be *extrapolated*
            csi_lltf_sensor[:-1:2] = lltf_all_cplx

            # If acquire_force_lltf is false, the real part of the last subcarrier is provided.
            # In that case, set real part of last subcarrier to the provided value, copy imaginary part from second last subcarrier.
            # Otherwise, extrapolate last subcarrier.
            if not serialized_csi.acquire_force_lltf:
                csi_lltf_sensor[-1] = final_re + 1.0j * csi_lltf_sensor[-3].imag
            else:
                csi_lltf_sensor[-1] = 2 * csi_lltf_sensor[-3] - csi_lltf_sensor[-5]

            # DC subcarrier
            # Only provided if acquire_force_lltf is true, otherwise needs to be interpolated
            if not serialized_csi.acquire_force_lltf:
                dc_subcarrier_index = csi.LEGACY_COEFFICIENTS_PER_CHANNEL // 2
                csi_lltf_sensor[dc_subcarrier_index] = (csi_lltf_sensor[dc_subcarrier_index - 2] + csi_lltf_sensor[dc_subcarrier_index + 2]) / 2.0

            # Interpolate to get full 53 subcarriers
            csi_lltf_sensor[1::2] = 0.5 * (csi_lltf_sensor[0:-1:2] + csi_lltf_sensor[2::2])

        self._foreach_complete_sensor(deserialize_lltf_packet)

        # Need to take timestamps into account to provide phase coherence across all sensors
        # TODO: For timestamp synchronization across datapoints, do not subtract mean, but use known reference point!
        delay = self.get_sensor_timestamps() - np.nanmean(self.get_sensor_timestamps())

        subcarrier_range = np.arange(-csi_lltf.shape[-1] // 2, csi_lltf.shape[-1] // 2)[np.newaxis, np.newaxis, np.newaxis, :]

        # Need to adjust range if using 40MHz wide channel since LO is either above or below the primary channel that L-LTF is on
        subcarrier_range -= self.get_secondary_channel_relative() * int(2 * constants.WIFI_CHANNEL_SPACING / constants.WIFI_SUBCARRIER_SPACING)

        # 128 bit delay is overkill here, CSI is only 2x32 bit, product would be 2x128 bit
        sto_delay_correction = np.exp(-1.0j * 2 * np.pi * delay[:, :, :, np.newaxis] * constants.WIFI_SUBCARRIER_SPACING * subcarrier_range).astype(np.complex64)
        csi_lltf = np.einsum("bras,bras->bras", csi_lltf, sto_delay_correction)

        return csi_lltf

    def deserialize_csi_ht20ltf(self):
        """
        Deserialize the HT20 (HT-LTF without channel bonding) part of the CSI data.

        :return: The HT-LTF part of the CSI data as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi.HT_COEFFICIENTS_PER_CHANNEL)`
        """
        assert self.has_ht20ltf()
        csi_ht20 = np.zeros(self.shape + (csi.HT_COEFFICIENTS_PER_CHANNEL,), dtype=np.complex64)

        def deserialize_ht20_packet(b, r, a, serialized_csi):
            nonlocal csi_ht20
            csi_ht20_sensor = csi_ht20[b, r, a, :].view()

            # The ESP32 provides CSI as int8_t values in (im, re) pairs (in this order!)
            # To go from the (re, im) interpretation to (im, re), compute conjugate and multiply by 1.0j.
            csi_ht20_sensor[:] = np.asarray(csi.csi_buf_v3_ht20_t(serialized_csi.buf).htltf, dtype=np.int8).astype(np.float32).view(np.complex64)
            csi_ht20_sensor[:] = -1.0j * np.conj(csi_ht20_sensor)

        self._foreach_complete_sensor(deserialize_ht20_packet)

        # Need to take timestamps into account to provide phase coherence across all sensors
        # TODO: For timestamp synchronization across datapoints, do not subtract mean, but use known reference point!
        delay = self.get_sensor_timestamps() - np.nanmean(self.get_sensor_timestamps())

        subcarrier_range = np.arange(-csi_ht20.shape[-1] // 2, csi_ht20.shape[-1] // 2)[np.newaxis, np.newaxis, np.newaxis, :]

        # Need to adjust range if using 40MHz wide channel since LO is either above or below the primary channel that HT20 is on
        subcarrier_range -= self.get_secondary_channel_relative() * int(2 * constants.WIFI_CHANNEL_SPACING / constants.WIFI_SUBCARRIER_SPACING)

        # 128 bit delay is overkill here, CSI is only 2x32 bit, product would be 2x128 bit
        sto_delay_correction = np.exp(-1.0j * 2 * np.pi * delay[:, :, :, np.newaxis] * constants.WIFI_SUBCARRIER_SPACING * subcarrier_range).astype(np.complex64)
        csi_ht20 = np.einsum("bras,bras->bras", csi_ht20, sto_delay_correction)
        return csi_ht20

    def deserialize_csi_ht40ltf(self):
        """
        Deserialize the HT40 (HT-LTF with channel bonding) part of the CSI data.

        :return: The HT-LTF part of the CSI data as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS + csi.HT_COEFFICIENTS_PER_CHANNEL)`
        """
        assert self.has_ht40ltf()
        loc = self.get_secondary_channel_relative()
        assert loc != 0

        csi_ht40 = np.zeros(
            self.shape + (csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS + csi.HT_COEFFICIENTS_PER_CHANNEL,),
            dtype=np.complex64,
        )

        def deserialize_ht40_packet(b, r, a, serialized_csi):
            nonlocal csi_ht40
            csi_ht40_sensor = csi_ht40[b, r, a, :].view()
            csi_ht40_sensor_higher = csi_ht40[b, r, a, : csi.HT_COEFFICIENTS_PER_CHANNEL].view()
            csi_ht40_sensor_lower = csi_ht40[b, r, a, -csi.HT_COEFFICIENTS_PER_CHANNEL :].view()

            # The ESP32 provides CSI as int8_t values in (im, re) pairs (in this order!)
            # To go from the (re, im) interpretation to (im, re), compute conjugate and multiply by 1.0j.
            csi_ht40_sensor_higher[:] = (
                np.asarray(
                    csi.csi_buf_v3_ht40_t(serialized_csi.buf).htltf_higher,
                    dtype=np.int8,
                )
                .astype(np.float32)
                .view(np.complex64)
            )
            csi_ht40_sensor_lower[:] = np.asarray(csi.csi_buf_v3_ht40_t(serialized_csi.buf).htltf_lower, dtype=np.int8).astype(np.float32).view(np.complex64)
            csi_ht40_sensor[:] = -1.0j * np.conj(csi_ht40_sensor)

        self._foreach_complete_sensor(deserialize_ht40_packet)

        # Secondary channel experiences phase shift by pi / 2
        # This is likely due to the pi / 2 phase shift specified for the pilot symbols,
        # see IEEE 80211-2012 section 20.3.9.3.4 L-LTF definition
        csi_ht40_higher = csi_ht40[:, :, :, : csi.HT_COEFFICIENTS_PER_CHANNEL].view()
        csi_ht40_lower = csi_ht40[:, :, :, -csi.HT_COEFFICIENTS_PER_CHANNEL :].view()

        if loc == 1:
            csi_ht40_higher[:] = csi_ht40_higher * np.exp(-1.0j * np.pi / 2)
        else:
            csi_ht40_lower[:] = csi_ht40_lower * np.exp(-1.0j * np.pi / 2)

        # Need to take timestamps into account to provide phase coherence across all sensors
        # TODO: For timestamp synchronization across datapoints, do not subtract mean, but use known reference point!
        delay = self.get_sensor_timestamps() - np.nanmean(self.get_sensor_timestamps())

        subcarrier_range = np.arange(-csi_ht40.shape[-1] // 2, csi_ht40.shape[-1] // 2)[np.newaxis, np.newaxis, np.newaxis, :]

        # 128 bit delay is overkill here, CSI is only 2x32 bit, product would be 2x128 bit
        sto_delay_correction = np.exp(-1.0j * 2 * np.pi * delay[:, :, :, np.newaxis] * constants.WIFI_SUBCARRIER_SPACING * subcarrier_range).astype(np.complex64)
        csi_ht40 = np.einsum("bras,bras->bras", csi_ht40, sto_delay_correction)

        return csi_ht40

    def has_lltf(self) -> bool:
        """
        Check if L-LTF channel estimates are available for all complete sensors.

        :return: True if there is L-LTF CSI data for all complete sensors, False otherwise
        """
        have_lltf_all = True

        def check_lltf(b, r, a, serialized_csi):
            nonlocal have_lltf_all
            match type(serialized_csi):
                case csi.serialized_csi_v1_t:
                    pass  # V1 always has L-LTF
                case csi.serialized_csi_v3_t:
                    # We only need to check this if acquire_force_lltf is false (otherwise, sensor always provides L-LTF)
                    if not serialized_csi.acquire_force_lltf:
                        # If force lltf is false, sensor module is configured to only provide L-LTF if frame is 802.11g
                        if not csi.wifi_pkt_rx_ctrl_v3_t(serialized_csi.rx_ctrl).cur_bb_format == csi.wifi_rx_bb_format_t.RX_BB_FORMAT_11G:
                            have_lltf_all = False

        self._foreach_complete_sensor(check_lltf)

        return have_lltf_all

    def has_ht20ltf(self) -> bool:
        """
        Check if HT20 (HT-LTF without channel bonding) channel estimates are available for all complete sensors.

        :return: True if there is HT20 CSI data for all complete sensors, False otherwise
        """
        have_ht20_all = True

        def check_ht20(b, r, a, serialized_csi):
            nonlocal have_ht20_all
            match type(serialized_csi):
                case csi.serialized_csi_v1_t:
                    pass  # TODO: Implement properly...
                case csi.serialized_csi_v3_t:
                    # If force lltf is true, sensor only provides L-LTF, never HT20-LTF
                    if serialized_csi.acquire_force_lltf:
                        have_ht20_all = False

                    if not csi.wifi_pkt_rx_ctrl_v3_t(serialized_csi.rx_ctrl).cur_bb_format == csi.wifi_rx_bb_format_t.RX_BB_FORMAT_HT:
                        have_ht20_all = False

        self._foreach_complete_sensor(check_ht20)

        return have_ht20_all

    def has_ht40ltf(self) -> bool:
        """
        Check if HT40 (HT-LTF with 40MHz channel bonding) channel estimates are available for all complete sensors.

        :return: True if there is HT40 CSI data for all complete sensors, False otherwise
        """
        have_ht40_all = True

        def check_ht40(b, r, a, serialized_csi):
            nonlocal have_ht40_all
            match type(serialized_csi):
                case csi.serialized_csi_v1_t:
                    if not csi.wifi_pkt_rx_ctrl_v1_t(serialized_csi.rx_ctrl).cwb == 1:
                        have_ht40_all = False
                case csi.serialized_csi_v3_t:
                    # If force lltf is true, sensor only provides L-LTF, never HT40-LTF
                    if serialized_csi.acquire_force_lltf:
                        have_ht40_all = False

                    # Check if packet is HT (HT20 or HT40)
                    if not csi.wifi_pkt_rx_ctrl_v3_t(serialized_csi.rx_ctrl).cur_bb_format == csi.wifi_rx_bb_format_t.RX_BB_FORMAT_HT:
                        have_ht40_all = False

                    # Check if channel bonding is used: he_siga1 is actuall ht_sig1, which contains the CWB bit at bit 7
                    if not (csi.wifi_pkt_rx_ctrl_v3_t(serialized_csi.rx_ctrl).he_siga1 & 0x80) != 0:
                        have_ht40_all = False

        self._foreach_complete_sensor(check_ht40)

        return have_ht40_all

    def get_secondary_channel_relative(self):
        """
        Get the relative position of the secondary channel with respect to the primary channel.

        :return: 0 if no secondary channel is used, 1 if the secondary channel is above the primary channel, -1 if the secondary channel is below the primary channel
        """
        match type(self._first_complete_sensor()):
            case csi.serialized_csi_v1_t:
                match csi.wifi_pkt_rx_ctrl_v1_t(self._first_complete_sensor().rx_ctrl).secondary_channel:
                    case 0:
                        return 0
                    case 1:
                        return 1
                    case 2:
                        return -1
            case csi.serialized_csi_v3_t:
                match csi.wifi_pkt_rx_ctrl_v3_t(self._first_complete_sensor().rx_ctrl).second:
                    case 0:
                        return 0
                    case 1:
                        return 1
                    case 2:
                        return -1

        raise ValueError("Unknown serialized_csi type or secondary channel value")

    def get_primary_channel(self) -> int:
        """
        Get the primary channel number.

        :return: The primary channel number
        """
        return csi.wifi_pkt_rx_ctrl_v3_t(self._first_complete_sensor().rx_ctrl).channel

    def get_secondary_channel(self) -> int:
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

        The age is only approximate, it is based on the timestamp when the :class:`.CSICluster` object was created,
        not on the sensor timestamps.

        :return: The age of the CSI data, in seconds
        """
        return time.time() - self.timestamp

    def get_sensor_timestamps(self):
        """
        Get the (nanosecond-precision) timestamps at which the WiFi packet was sampled by the sensors.
        This timestamp does not include the offset that the chip derived from the CSI, it is only the sampling start time.

        :return: A numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)` that contains the sensor timestamps in seconds
        """
        sensor_timestamps = np.full(self.shape, np.nan, dtype=np.float128)

        def append_sensor_timestamp(b, r, a, serialized_csi):
            timestamp_ns = np.float128(self._nanosecond_timestamp(serialized_csi))
            sensor_timestamps[b, r, a] = np.float128(timestamp_ns) / 1e9

        self._foreach_complete_sensor(append_sensor_timestamp)
        return sensor_timestamps

    def get_host_timestamp(self):
        """
        Get the timestamp at which the :class:`.CSICluster` object was created, which is approximately when the first sensor received the CSI data.

        :return: The timestamp at which the first sensor received the CSI data, in seconds since the epoch
        """
        return self.timestamp

    def get_rssi(self):
        """
        Get the RSSI values of the WiFi packet.
        """
        return self.rssi_all

    def get_rfswitch_state(self):
        """
        Get the RF switch state of all sensors when the WiFi packet was received.
        """
        return self.rfswitch_state_all

    def get_source_mac(self):
        """
        Get the source MAC address of the WiFi packet.

        :return: The source MAC address of the WiFi packet
        """
        return self.source_mac

    def get_noise_floor(self):
        """
        Get the noise floor of the WiFi packet.

        :return: The noise floor of the WiFi packet
        """
        return self.noise_floor_all

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
        rxstart_time_cyc = csi.wifi_pkt_rx_ctrl_v3_t(serialized_csi.rx_ctrl).rxstart_time_cyc
        # rxstart_time_cyc_dec = csi.wifi_pkt_rx_ctrl_v3_t(serialized_csi.rx_ctrl).rxstart_time_cyc_dec
        # rxstart_time_cyc_dec = 2048 - rxstart_time_cyc_dec if rxstart_time_cyc_dec >= 1024 else rxstart_time_cyc_dec

        # Backwards compatibility: Only use global timestamp if it is nonzero
        us_timestamp = serialized_csi.timestamp
        if serialized_csi.global_timestamp_us != 0:
            us_timestamp = serialized_csi.global_timestamp_us
        hw_latched_timestamp_ns = us_timestamp * 1000

        # "official" formula by Espressif:
        # timestamp_ns = np.float128(serialized_csi.timestamp * 1000 + ((rxstart_time_cyc * 12500) // 1000) + ((rxstart_time_cyc_dec * 1562) // 1000) - 20800)
        # Formula that is probably more accurate:
        CYC_PERIOD_NS = 1 / 80e6 * 1e9
        # CYC_DEC_PERIOD_NS = 1/640e6*1e9
        HW_TIMESTAMP_LAG_NS = 20800
        return hw_latched_timestamp_ns - HW_TIMESTAMP_LAG_NS + rxstart_time_cyc * CYC_PERIOD_NS  # + rxstart_time_cyc_dec * CYC_DEC_PERIOD_NS
