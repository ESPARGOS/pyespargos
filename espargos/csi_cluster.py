#!/usr/bin/env python

"""Store the CSI reported by multiple sensors for one Wi-Fi packet."""

import binascii
import time

import numpy as np

from . import revisions
from . import constants
from . import csi_compression
from . import csi_packet
from . import radar_packet
from . import sensor
from . import csi_processing
from . import wifi
from .sensor_cluster import ClusterCollisionError, SensorCluster

__all__ = ["CSICluster"]

_RAW_LLTF_BYTES = csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL * 2
_RAW_HT20_BYTES = csi_packet.HT_COEFFICIENTS_PER_CHANNEL * 2
_RAW_HT40_BYTES = (csi_packet.HT_COEFFICIENTS_PER_CHANNEL * 2) + (csi_packet.HT40_GAP_SUBCARRIERS * 2) + (csi_packet.HT_COEFFICIENTS_PER_CHANNEL * 2)
_RAW_HE20_BYTES = csi_packet.HE20_COEFFICIENTS_PER_CHANNEL * 2


class CSICluster(SensorCluster):
    """
    CSI reported by the sensor array for one Wi-Fi packet.

    Sensors report the packet independently. This class places each report at
    its ``(board, row, column)`` position so consumers receive one array-shaped
    measurement. A cluster may additionally contain the transmit-side report
    for a radar packet. CSI can come from ordinary over-the-air traffic or from
    packets used during calibration.
    """

    def __init__(
        self,
        frame_key: wifi.WiFiFrameKey,
        board_revisions: list[revisions.BoardRevision],
    ):
        """
        Constructor for the CSICluster class.

        All channel coefficients added to this class belong to the same WiFi packet,
        so they share the same source and destination MAC addresses and sequence control field.
        The constructor pre-allocates memory for the CSI data.

        :param frame_key: Fields identifying the WiFi packet
        :param board_revisions: The ESPARGOS board revisions in the pool
        """
        super().__init__(board_revisions)
        self.frame_key = frame_key
        self.source_mac = frame_key.source_mac.hex()
        self.destination_mac = frame_key.destination_mac.hex()
        self.sequence_control = wifi.SequenceControl(b"\x00\x00")
        self.sequence_control.seg = frame_key.sequence_number
        self.sequence_control.frag = frame_key.fragment_number
        self.retry = frame_key.retry

        self._host_timestamp = time.time()
        self._csi_packets = [[[None for _ in range(constants.ANTENNAS_PER_ROW)] for _ in range(constants.ROWS_PER_BOARD)] for _ in self.board_revisions]
        self._radar_tx_report = None
        self._radar_tx_index = -1

        # Allocate memory for the RSSI, gain, rf switch state and noise floor values
        self._rssi = np.full(self.shape, fill_value=np.nan, dtype=np.float32)
        self._rx_gain = np.full(self.shape, fill_value=np.nan, dtype=np.float32)
        self._fft_gain = np.full(self.shape, fill_value=np.nan, dtype=np.float32)
        self._rf_switch_state = np.full(self.shape, fill_value=sensor.RFSwitchState.SENSOR_RFSWITCH_UNKNOWN, dtype=np.uint8)
        self._noise_floor = np.full(self.shape, fill_value=np.nan, dtype=np.float32)
        self._cfo = np.full(self.shape, fill_value=np.nan, dtype=np.float32)
        self._lltf_8bit_mode = np.full(self.shape, fill_value=False, dtype=np.bool_)
        self._gain_table_entry_raw = np.zeros(self.shape + (12,), dtype=np.uint8)
        self._gain_table_entry_valid = np.full(self.shape, fill_value=False)

    def get_csi_packet(self, board_index: int, esp_num: int) -> csi_packet.CSIPacket | None:
        """Return the CSI packet already stored for one sensor, if any."""

        row, col = self.board_revisions[board_index].esp_num_to_row_col(esp_num)
        return self._csi_packets[board_index][row][col]

    def add_message(self, board_index: int, sensor_message: sensor.SensorMessage) -> bool:
        """Add CSI or radar metadata for this cluster's Wi-Fi frame."""

        stream_packet = sensor_message.payload
        board_index, row, col = self.get_sensor_position(board_index, sensor_message.antenna_id)

        if isinstance(stream_packet, csi_packet.CSIPacket):
            existing_csi = self._csi_packets[board_index][row][col]
            if existing_csi is not None:
                if bytes(existing_csi) == bytes(stream_packet):
                    return False
                # The Retry bit distinguishes an original transmission from
                # its retries, but not multiple retry attempts. Retain the
                # first retry observed by this sensor and silently discard
                # later attempts under the same key.
                if self.retry:
                    return False
                raise ClusterCollisionError(f"conflicting CSI from board {board_index}, row {row}, column {col}")
            if np.any(self.completion) and self.is_radar != stream_packet.is_radar:
                raise ClusterCollisionError("radar and non-radar CSI use the same Wi-Fi frame key")
            if self.has_radar_tx_report and not stream_packet.is_radar:
                raise ClusterCollisionError("non-radar CSI conflicts with an existing radar TX report")
            self._add_csi((board_index, row, col), stream_packet)
            self._mark_sensor_position_complete((board_index, row, col))
            return True

        if isinstance(stream_packet, radar_packet.RadarTxReportPacket):
            existing_report = self.radar_tx_report
            if existing_report is not None:
                if bytes(existing_report) == bytes(stream_packet):
                    return False
                raise ClusterCollisionError(f"conflicting radar TX report with tx_count={stream_packet.tx_count}")
            if np.any(self.completion) and not self.is_radar:
                raise ClusterCollisionError("radar TX report conflicts with existing non-radar CSI")
            self._set_radar_tx_report(
                stream_packet,
                sensor_position=(board_index, row, col),
            )
            return True

        raise TypeError(f"Unsupported CSICluster sensor-message payload: {type(stream_packet).__name__}")

    def _add_csi(
        self,
        sensor_position: tuple[int, int, int],
        serialized_csi: csi_packet.CSIPacket,
    ):
        """
        Add CSI data to the cluster.

        :param sensor_position: The logical ``(board, row, column)`` position
        :param serialized_csi: The serialized CSI data
        """
        assert binascii.hexlify(bytearray(serialized_csi.source_mac)).decode("utf-8") == self.source_mac
        assert binascii.hexlify(bytearray(serialized_csi.destination_mac)).decode("utf-8") == self.destination_mac
        assert serialized_csi.sequence_control.seg == self.sequence_control.seg
        assert serialized_csi.sequence_control.frag == self.sequence_control.frag
        assert serialized_csi.is_retry == self.retry
        if self._radar_tx_report is not None:
            assert serialized_csi.is_radar

        board_index, row, col = sensor_position

        # Store CSI data to pre-allocated memory
        self._csi_packets[board_index][row][col] = serialized_csi
        # self.complex_csi_all[board_num, row, col] = csi_cplx # TODO: Will not work for V3 :(

        # Signed metadata fields are stored as unsigned due to ctypes packing limitations.
        rx_ctrl = csi_packet.WiFiPacketRxControlV3(serialized_csi.rx_ctrl)
        self._rssi[board_index, row, col] = rx_ctrl.rssi_signed
        self._rx_gain[board_index, row, col] = rx_ctrl.rx_gain_signed
        self._fft_gain[board_index, row, col] = rx_ctrl.fft_gain_signed
        self._noise_floor[board_index, row, col] = rx_ctrl.noise_floor_signed
        self._rf_switch_state[board_index, row, col] = serialized_csi.rf_switch_state
        self._cfo[board_index, row, col] = rx_ctrl.cfo
        self._lltf_8bit_mode[board_index, row, col] = serialized_csi.acquire_lltf_8bit_mode
        self._gain_table_entry_valid[board_index, row, col] = bool(serialized_csi.gain_table_entry_valid)
        if serialized_csi.gain_table_entry_valid:
            self._gain_table_entry_raw[board_index, row, col, :] = np.frombuffer(serialized_csi.gain_table_entry_raw, dtype=np.uint8)

    def _set_radar_tx_report(
        self,
        radar_tx_report: radar_packet.RadarTxReportPacket,
        sensor_position: tuple[int, int, int] | None = None,
    ):
        """
        Attach radar transmit metadata for the Wi-Fi packet represented by this cluster.
        """
        assert binascii.hexlify(bytearray(radar_tx_report.source_mac)).decode("utf-8") == self.source_mac
        assert binascii.hexlify(bytearray(radar_tx_report.destination_mac)).decode("utf-8") == self.destination_mac
        assert radar_tx_report.sequence_control.seg == self.sequence_control.seg
        assert radar_tx_report.sequence_control.frag == self.sequence_control.frag

        serialized_csi = self._first_complete_sensor()
        if serialized_csi is not None:
            assert serialized_csi.is_radar

        if self._radar_tx_report is not None:
            assert bytes(self._radar_tx_report) == bytes(radar_tx_report)

        self._radar_tx_report = radar_tx_report
        if sensor_position is not None:
            board_index, row, col = sensor_position
            self._radar_tx_index = board_index * constants.ROWS_PER_BOARD * constants.ANTENNAS_PER_ROW + row * constants.ANTENNAS_PER_ROW + col

    @property
    def has_radar_tx_report(self) -> bool:
        """
        Check whether this cluster has transmit-side radar metadata attached.
        """
        return self._radar_tx_report is not None

    @property
    def radar_tx_report(self):
        """
        Return the transmit-side radar report for this packet, or None if not available.
        """
        return self._radar_tx_report

    @property
    def radar_tx_index(self) -> int:
        """
        Return the flattened TX sensor index derived from the sensor-stream UID, or -1 if unknown.
        """
        return int(self._radar_tx_index)

    def deserialize_csi_lltf(self):
        """
        Deserialize the L-LTF part of the CSI data.

        :return: The L-LTF part of the CSI data as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL)`
        """
        assert self.has_lltf

        csi_lltf = np.zeros(self.shape + (csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL,), dtype=np.complex64)

        def deserialize_lltf_packet(b, r, a, serialized_csi):
            nonlocal csi_lltf
            csi_lltf_sensor = csi_lltf[b, r, a, :].view()

            if serialized_csi.is_compressed:
                csi_lltf_sensor[:] = csi_compression.decode_compressed_lltf(
                    serialized_csi.buf,
                    serialized_csi.acquire_force_lltf,
                    serialized_csi.acquire_lltf_8bit_mode,
                )
                return

            if serialized_csi.acquire_lltf_8bit_mode:
                csi_lltf_sensor[:] = serialized_csi._decode_lltf8()
                csi_processing.interpolate_lltf_gap(csi_lltf_sensor)
            elif serialized_csi.acquire_force_lltf:
                # In forced LLTF mode the ESP32-C61 reports 52 signed 12-bit values:
                # 26 complex coefficients for every second subcarrier, including DC.
                # The last active subcarrier is not measured and must be extrapolated.
                lltf_all = serialized_csi._decode_lltf12(52)
                csi_lltf_sensor[:-1:2] = lltf_all.astype(np.float32).view(np.complex64)
                csi_lltf_sensor[-1] = 2 * csi_lltf_sensor[-3] - csi_lltf_sensor[-5]
            else:
                # Native 11g LLTF carries 26 complex coefficients for even-indexed
                # subcarriers plus a final real-only sample for the last subcarrier.
                lltf_all = serialized_csi._decode_lltf12(53)
                even_coeffs = lltf_all[:52].astype(np.float32).view(np.complex64)
                csi_lltf_sensor[0:52:2] = even_coeffs
                csi_lltf_sensor[-1] = lltf_all[52].astype(np.float32) + 1.0j * csi_lltf_sensor[-3].imag

            # DC subcarrier. In sparse 12-bit LLTF this is only provided when
            # force LLTF is true. 8-bit LLTF was handled above like HT20.
            if not serialized_csi.acquire_force_lltf and not serialized_csi.acquire_lltf_8bit_mode:
                dc_subcarrier_index = csi_packet.LEGACY_COEFFICIENTS_PER_CHANNEL // 2
                csi_lltf_sensor[dc_subcarrier_index] = (csi_lltf_sensor[dc_subcarrier_index - 2] + csi_lltf_sensor[dc_subcarrier_index + 2]) / 2.0

            # Interpolate to get full 53 subcarriers
            if not serialized_csi.acquire_lltf_8bit_mode:
                csi_lltf_sensor[1::2] = 0.5 * (csi_lltf_sensor[0:-1:2] + csi_lltf_sensor[2::2])

        self._foreach_complete_sensor(deserialize_lltf_packet)

        # Need to take timestamps into account to provide phase coherence across all sensors
        delay = self.sensor_timestamps
        subcarrier_range = csi_packet.get_csi_format_subcarrier_indices("lltf").astype(np.float64)[np.newaxis, np.newaxis, np.newaxis, :]

        # Need to adjust range if using 40MHz wide channel since LO is either above or below the primary channel that L-LTF is on
        subcarrier_range -= self.secondary_channel_relative * int(2 * constants.WIFI_CHANNEL_SPACING / constants.WIFI_SUBCARRIER_SPACING)
        sto_delay_correction = np.exp(-1.0j * 2 * np.pi * delay[:, :, :, np.newaxis] * constants.WIFI_SUBCARRIER_SPACING * subcarrier_range)
        csi_lltf = np.einsum("bras,bras->bras", csi_lltf, sto_delay_correction)

        return csi_lltf

    def deserialize_csi_ht20ltf(self):
        """
        Deserialize the HT20 (HT-LTF without channel bonding) part of the CSI data.

        :return: The HT-LTF part of the CSI data as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi_packet.HT_COEFFICIENTS_PER_CHANNEL)`
        """
        assert self.has_ht20ltf
        csi_ht20 = np.zeros(self.shape + (csi_packet.HT_COEFFICIENTS_PER_CHANNEL,), dtype=np.complex64)

        def deserialize_ht20_packet(b, r, a, serialized_csi):
            nonlocal csi_ht20
            csi_ht20_sensor = csi_ht20[b, r, a, :].view()

            if serialized_csi.is_compressed:
                csi_ht20_sensor[:] = csi_compression.decode_compressed_ht20(serialized_csi.buf)
                return

            # The ESP32 provides CSI as int8_t values in (im, re) pairs (in this order!)
            # To go from the (re, im) interpretation to (im, re), compute conjugate and multiply by 1.0j.
            # If channel bonding is used, provide CSI of primary channel
            if csi_packet.WiFiPacketRxControlV3(serialized_csi.rx_ctrl).he_siga1 & 0x80 != 0:
                ht40_bytes = np.frombuffer(serialized_csi.buf[:_RAW_HT40_BYTES], dtype=np.int8)
                htltf_lower = ht40_bytes[:_RAW_HT20_BYTES]
                htltf_higher = ht40_bytes[_RAW_HT20_BYTES + (csi_packet.HT40_GAP_SUBCARRIERS * 2) : _RAW_HT40_BYTES]
                primary = htltf_higher if self.secondary_channel_relative == -1 else htltf_lower
                csi_ht20_sensor[:] = np.asarray(primary, dtype=np.int8).astype(np.float32).view(np.complex64)
            else:
                csi_ht20_sensor[:] = np.frombuffer(serialized_csi.buf[:_RAW_HT20_BYTES], dtype=np.int8).astype(np.float32).view(np.complex64)
            csi_ht20_sensor[:] = -1.0j * np.conj(csi_ht20_sensor)

        self._foreach_complete_sensor(deserialize_ht20_packet)

        # Need to take timestamps into account to provide phase coherence across all sensors
        delay = self.sensor_timestamps
        subcarrier_range = csi_packet.get_csi_format_subcarrier_indices("ht20").astype(np.float64)[np.newaxis, np.newaxis, np.newaxis, :]

        # Need to adjust range if using 40MHz wide channel since LO is either above or below the primary channel that HT20 is on
        subcarrier_range -= self.secondary_channel_relative * int(2 * constants.WIFI_CHANNEL_SPACING / constants.WIFI_SUBCARRIER_SPACING)

        # 128 bit delay is overkill here, CSI is only 2x32 bit, product would be 2x128 bit
        sto_delay_correction = np.exp(-1.0j * 2 * np.pi * delay[:, :, :, np.newaxis] * constants.WIFI_SUBCARRIER_SPACING * subcarrier_range)
        csi_ht20 = np.einsum("bras,bras->bras", csi_ht20, sto_delay_correction)
        return csi_ht20

    def deserialize_csi_ht40ltf(self):
        """
        Deserialize the HT40 (HT-LTF with channel bonding) part of the CSI data.

        :return: The HT-LTF part of the CSI data as a complex-valued numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW, csi_packet.HT_COEFFICIENTS_PER_CHANNEL + csi_packet.HT40_GAP_SUBCARRIERS + csi_packet.HT_COEFFICIENTS_PER_CHANNEL)`
        """
        assert self.has_ht40ltf
        loc = self.secondary_channel_relative
        assert loc != 0

        csi_ht40 = np.zeros(
            self.shape + (csi_packet.HT_COEFFICIENTS_PER_CHANNEL + csi_packet.HT40_GAP_SUBCARRIERS + csi_packet.HT_COEFFICIENTS_PER_CHANNEL,),
            dtype=np.complex64,
        )

        def deserialize_ht40_packet(b, r, a, serialized_csi):
            nonlocal csi_ht40
            csi_ht40_sensor = csi_ht40[b, r, a, :].view()
            csi_ht40_sensor_lower = csi_ht40[b, r, a, : csi_packet.HT_COEFFICIENTS_PER_CHANNEL].view()
            csi_ht40_sensor_higher = csi_ht40[b, r, a, -csi_packet.HT_COEFFICIENTS_PER_CHANNEL :].view()

            if serialized_csi.is_compressed:
                csi_ht40_sensor[:] = csi_compression.decode_compressed_ht40(serialized_csi.buf)
                return

            # The ESP32 provides CSI as int8_t values in (im, re) pairs (in this order!)
            # To go from the (re, im) interpretation to (im, re), compute conjugate and multiply by 1.0j.
            ht40_bytes = np.frombuffer(serialized_csi.buf[:_RAW_HT40_BYTES], dtype=np.int8)
            csi_ht40_sensor_higher[:] = ht40_bytes[_RAW_HT20_BYTES + (csi_packet.HT40_GAP_SUBCARRIERS * 2) : _RAW_HT40_BYTES].astype(np.float32).view(np.complex64)
            csi_ht40_sensor_lower[:] = ht40_bytes[:_RAW_HT20_BYTES].astype(np.float32).view(np.complex64)
            csi_ht40_sensor[:] = -1.0j * np.conj(csi_ht40_sensor)

        self._foreach_complete_sensor(deserialize_ht40_packet)

        # Secondary channel experiences phase shift by pi / 2
        # This is likely due to the pi / 2 phase shift specified for the pilot symbols,
        # see IEEE 80211-2012 section 20.3.9.3.4 L-LTF definition
        csi_ht40_higher = csi_ht40[:, :, :, : csi_packet.HT_COEFFICIENTS_PER_CHANNEL].view()
        csi_ht40_higher[:] = csi_ht40_higher * np.exp(1.0j * np.pi / 2)

        # Need to take timestamps into account to provide phase coherence across all sensors
        delay = self.sensor_timestamps
        subcarrier_range = csi_packet.get_csi_format_subcarrier_indices("ht40").astype(np.float64)[np.newaxis, np.newaxis, np.newaxis, :]
        sto_delay_correction = np.exp(-1.0j * 2 * np.pi * delay[:, :, :, np.newaxis] * constants.WIFI_SUBCARRIER_SPACING * subcarrier_range)
        csi_ht40 = np.einsum("bras,bras->bras", csi_ht40, sto_delay_correction)

        return csi_ht40

    def deserialize_csi_he20ltf(self):
        """
        Deserialize the HE20 HE-LTF part of the CSI data.

        The internal HE20 ordering is ascending subcarrier index ``-122..122``.
        The invalid / null tones ``-1, 0, 1`` are explicitly zeroed because
        the raw PHY payload may contain meaningless values there.
        """
        assert self.has_he20ltf
        csi_he20 = np.zeros(self.shape + (csi_packet.HE20_COEFFICIENTS_PER_CHANNEL,), dtype=np.complex64)

        def deserialize_he20_packet(b, r, a, serialized_csi):
            nonlocal csi_he20
            csi_he20_sensor = csi_he20[b, r, a, :].view()

            if serialized_csi.is_compressed:
                csi_he20_sensor[:] = csi_compression.decode_compressed_he20(serialized_csi.buf)
                return

            he20_raw = np.frombuffer(serialized_csi.buf[:_RAW_HE20_BYTES], dtype=np.int8).astype(np.float32).view(np.complex64)
            csi_he20_sensor[:] = -1.0j * np.conj(he20_raw)

        self._foreach_complete_sensor(deserialize_he20_packet)

        delay = self.sensor_timestamps
        he20_fractional_delay = self._get_he20_fractional_timestamp_offsets()
        subcarrier_range = csi_packet.get_csi_format_subcarrier_indices("he20").astype(np.float64)[np.newaxis, np.newaxis, np.newaxis, :]
        sto_delay_correction = np.exp(-1.0j * 2 * np.pi * (delay + he20_fractional_delay)[:, :, :, np.newaxis] * (constants.WIFI_SUBCARRIER_SPACING / 4.0) * subcarrier_range)
        csi_he20 = np.einsum("bras,bras->bras", csi_he20, sto_delay_correction)
        csi_he20[..., 121:124] = 0.0
        return csi_he20

    @property
    def has_lltf(self) -> bool:
        """
        Check if L-LTF channel estimates are available for all complete sensors.

        :return: True if there is L-LTF CSI data for all complete sensors, False otherwise
        """
        have_lltf_all = True

        def check_lltf(b, r, a, serialized_csi):
            nonlocal have_lltf_all
            if serialized_csi.csi_len == 0:
                have_lltf_all = False
                return
            # We only need to check this if acquire_force_lltf is false (otherwise, sensor always provides L-LTF)
            if not serialized_csi.acquire_force_lltf:
                # If force lltf is false, sensor module is configured to only provide L-LTF if frame is 802.11g
                if not csi_packet.WiFiPacketRxControlV3(serialized_csi.rx_ctrl).cur_bb_format == csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_11G:
                    have_lltf_all = False

        self._foreach_complete_sensor(check_lltf)

        return have_lltf_all

    @property
    def has_ht20ltf(self) -> bool:
        """
        Check if HT20 (HT-LTF without channel bonding) channel estimates are available for all complete sensors.

        :return: True if there is HT20 CSI data for all complete sensors, False otherwise
        """
        have_ht20_all = True

        def check_ht20(b, r, a, serialized_csi):
            nonlocal have_ht20_all
            if serialized_csi.csi_len == 0:
                have_ht20_all = False
                return
            # If force lltf is true, sensor only provides L-LTF, never HT20-LTF
            if serialized_csi.acquire_force_lltf:
                have_ht20_all = False

            if not csi_packet.WiFiPacketRxControlV3(serialized_csi.rx_ctrl).cur_bb_format == csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_HT:
                have_ht20_all = False

        self._foreach_complete_sensor(check_ht20)

        return have_ht20_all

    @property
    def has_ht40ltf(self) -> bool:
        """
        Check if HT40 (HT-LTF with 40MHz channel bonding) channel estimates are available for all complete sensors.

        :return: True if there is HT40 CSI data for all complete sensors, False otherwise
        """
        have_ht40_all = True

        def check_ht40(b, r, a, serialized_csi):
            nonlocal have_ht40_all
            if serialized_csi.csi_len == 0:
                have_ht40_all = False
                return
            # If force lltf is true, sensor only provides L-LTF, never HT40-LTF
            if serialized_csi.acquire_force_lltf:
                have_ht40_all = False

            # Check if packet is HT (HT20 or HT40)
            if not csi_packet.WiFiPacketRxControlV3(serialized_csi.rx_ctrl).cur_bb_format == csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_HT:
                have_ht40_all = False

            # Check if channel bonding is used: he_siga1 is actuall ht_sig1, which contains the CWB bit at bit 7
            if csi_packet.WiFiPacketRxControlV3(serialized_csi.rx_ctrl).he_siga1 & 0x80 == 0:
                have_ht40_all = False

        self._foreach_complete_sensor(check_ht40)

        return have_ht40_all

    @property
    def has_he20ltf(self) -> bool:
        """
        Check if HE20 HE-LTF channel estimates are available for all complete sensors.
        """
        have_he20_all = True

        def check_he20(b, r, a, serialized_csi):
            nonlocal have_he20_all
            if serialized_csi.csi_len == 0:
                have_he20_all = False
                return
            if serialized_csi.acquire_force_lltf:
                have_he20_all = False
                return

            rx_ctrl = csi_packet.WiFiPacketRxControlV3(serialized_csi.rx_ctrl)
            if not self._is_he_format(rx_ctrl.cur_bb_format):
                have_he20_all = False
                return
            if rx_ctrl.second != 0:
                have_he20_all = False
                return
            if csi_packet.WiFiPacketRxControlV3(serialized_csi.rx_ctrl).rx_channel_estimate_len < _RAW_HE20_BYTES:
                have_he20_all = False

        self._foreach_complete_sensor(check_he20)

        return have_he20_all

    @property
    def secondary_channel_relative(self):
        """
        Get the relative position of the secondary channel with respect to the primary channel.

        :return: 0 if no secondary channel is used, 1 if the secondary channel is above the primary channel, -1 if the secondary channel is below the primary channel
        """
        # 802.11b packets: No secondary channel, return 0
        if csi_packet.WiFiPacketRxControlV3(self._first_complete_sensor().rx_ctrl).cur_bb_format == csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_11B:
            return 0

        match csi_packet.WiFiPacketRxControlV3(self._first_complete_sensor().rx_ctrl).second:
            case 0:
                return 0
            case 1:
                return 1
            case 2:
                return -1

        raise ValueError("Unknown secondary channel value")

    @property
    def primary_channel(self) -> int:
        """
        Get the primary channel number.

        :return: The primary channel number
        """
        return csi_packet.WiFiPacketRxControlV3(self._first_complete_sensor().rx_ctrl).channel

    @property
    def is_11b(self) -> bool:
        """
        Check whether this packet uses the 802.11b baseband format.

        :return: True if the packet is 802.11b, False otherwise
        """
        return csi_packet.WiFiPacketRxControlV3(self._first_complete_sensor().rx_ctrl).cur_bb_format == csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_11B

    @property
    def secondary_channel(self) -> int:
        """
        Get the secondary channel number.

        :return: The secondary channel number
        """
        return self.primary_channel + 4 * self.secondary_channel_relative

    @property
    def sensor_timestamps(self):
        """
        Get the (nanosecond-precision) timestamps at which the WiFi packet was sampled by the sensors.
        This timestamp does not include the offset that the chip derived from the CSI, it is only the sampling start time.

        :return: A numpy array of shape :code:`(boardcount, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)` that contains the sensor timestamps in seconds
        """
        sensor_timestamps = np.full(self.shape, np.nan, dtype=np.float64)

        def append_sensor_timestamp(b, r, a, serialized_csi):
            timestamp_ns = np.float64(self._nanosecond_timestamp(serialized_csi))
            sensor_timestamps[b, r, a] = np.float64(timestamp_ns) / 1e9

        self._foreach_complete_sensor(append_sensor_timestamp)
        return sensor_timestamps

    @property
    def host_timestamp(self):
        """
        Get the timestamp at which the :class:`.CSICluster` object was created, which is approximately when the first sensor received the CSI data.

        :return: The timestamp at which the first sensor received the CSI data, in seconds since the epoch
        """
        return self._host_timestamp

    @property
    def rssi(self):
        """
        Get the RSSI values of the WiFi packet.
        """
        return self._rssi

    @property
    def rx_gain(self):
        """
        Get the signed RX gain values reported for the WiFi packet.
        """
        return self._rx_gain

    @property
    def fft_gain(self):
        """
        Get the signed FFT gain values reported for the WiFi packet.
        """
        return self._fft_gain

    @property
    def rf_switch_state(self):
        """
        Get the RF switch state of all sensors when the WiFi packet was received.
        """
        return self._rf_switch_state

    @property
    def cfo(self):
        """
        Get the CFO values decoded from the sensor rx_ctrl metadata.
        """
        return self._cfo

    @property
    def lltf_8bit_mode(self):
        """
        Get whether each sensor reported LLTF CSI in 8-bit mode for this packet.
        """
        return self._lltf_8bit_mode

    @property
    def gain_table_entry_raw(self):
        """
        Get the raw 12-byte ESP32-C61 PHY gain-table entry used for each received packet.
        """
        return self._gain_table_entry_raw

    @property
    def gain_table_entry_valid(self):
        """
        Return a mask indicating whether a raw gain-table entry was reported for each sensor.
        """
        return self._gain_table_entry_valid

    @property
    def is_radar(self) -> bool:
        """
        Check whether this cluster corresponds to a radar packet.
        """
        serialized_csi = self._first_complete_sensor()
        return False if serialized_csi is None else serialized_csi.is_radar

    @property
    def is_calibration(self) -> bool:
        """
        Check whether this cluster corresponds to a calibration packet.
        """
        serialized_csi = self._first_complete_sensor()
        return False if serialized_csi is None else serialized_csi.is_calibration

    @property
    def noise_floor(self):
        """
        Get the noise floor of the WiFi packet.

        :return: The noise floor of the WiFi packet
        """
        return self._noise_floor

    # Internal helper functions
    def _foreach_complete_sensor(self, callback):
        for b, board in enumerate(self._csi_packets):
            for r, row in enumerate(board):
                for a, serialized_csi in enumerate(row):
                    if serialized_csi is not None:
                        callback(b, r, a, serialized_csi)

    def _first_complete_sensor(self):
        for board in self._csi_packets:
            for row in board:
                for serialized_csi in row:
                    if serialized_csi is not None:
                        return serialized_csi

        return None

    def _nanosecond_timestamp(self, serialized_csi):
        rxstart_time_cyc = csi_packet.WiFiPacketRxControlV3(serialized_csi.rx_ctrl).rxstart_time_cyc

        hw_latched_timestamp_ns = serialized_csi.global_timestamp_us * 1000

        # "official" formula by Espressif:
        # timestamp_ns = np.float128(serialized_csi.timestamp * 1000 + ((rxstart_time_cyc * 12500) // 1000) + ((rxstart_time_cyc_dec * 1562) // 1000) - 20800)
        # Formula that is probably more accurate:
        CYC_PERIOD_NS = 1 / 80e6 * 1e9
        HW_TIMESTAMP_LAG_NS = 20800
        return hw_latched_timestamp_ns - HW_TIMESTAMP_LAG_NS + rxstart_time_cyc * CYC_PERIOD_NS

    def _get_he20_fractional_timestamp_offsets(self):
        fractional_offsets = np.full(self.shape, np.nan, dtype=np.float64)

        def append_fractional_offset(b, r, a, serialized_csi):
            rxstart_time_cyc_dec = csi_packet.WiFiPacketRxControlV3(serialized_csi.rx_ctrl).rxstart_time_cyc_dec
            rxstart_time_cyc_dec = 2048 - rxstart_time_cyc_dec if rxstart_time_cyc_dec >= 1024 else rxstart_time_cyc_dec
            fractional_offsets[b, r, a] = float(rxstart_time_cyc_dec) / 640e6

        self._foreach_complete_sensor(append_fractional_offset)
        return fractional_offsets

    @staticmethod
    def _is_he_format(bb_format: int) -> bool:
        return bb_format in (
            csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_HE_SU,
            csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_HE_MU,
            csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_HE_ERSU,
            csi_packet.WiFiRxBasebandFormat.RX_BB_FORMAT_HE_TB,
        )
