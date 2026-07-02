#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import espargos
import espargos.constants
import espargos.radar
import numpy as np
import scipy.fft
from PyQt6 import QtCore, QtQml, QtWidgets
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtQuick import QQuickPaintedItem
from matplotlib import colormaps
import threading

from demos.common import ESPARGOSApplication, RadarControlMixin, RADAR_CONFIG_DEFAULTS

BUFFER_LENGTH = 1000
SUBCARRIERS = 53  # initial L-LTF subcarrier count; the DSP adapts to the received RX format

# Number of slow-time samples (packets) used for the Doppler FFT. Also determines the Doppler
# frequency resolution, so it must stay consistent between spacing_doppler and the FFT below.
NUM_DOPPLER_PACKETS = 101

SPEED_OF_LIGHT = 299792458
CARRIER_FREQUENCY = 2.432e9


class QImageTexture(QQuickPaintedItem):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidth(10)
        self.setHeight(10)
        texture_data = np.zeros((10, 10, 4), dtype=np.float32)
        self.image_data = np.ascontiguousarray((texture_data * 255).astype(np.uint8))
        self.image = QImage(self.image_data.data, 10, 10, 40, QImage.Format.Format_RGBA8888)

    @QtCore.pyqtSlot()
    def update_texture(self):
        texture_data = QtWidgets.QApplication.instance().data
        x = texture_data.shape[1]
        y = texture_data.shape[0]
        self.setWidth(x)
        self.setHeight(y)
        self.image_data = np.ascontiguousarray((texture_data * 255).astype(np.uint8))
        self.image = QImage(self.image_data.data, x, y, 4 * x, QImage.Format.Format_RGBA8888)
        self.update()

    def paint(self, painter: QPainter):
        painter.drawImage(0, 0, self.image)


class DopplerDelayApp(RadarControlMixin, ESPARGOSApplication):
    dataChanged = QtCore.pyqtSignal()
    statusUpdate = QtCore.pyqtSignal()
    # Emitted when the range/Doppler axis scaling changes (RX format or TX interval)
    axisScaleChanged = QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        **RADAR_CONFIG_DEFAULTS,
        "delay_oversampling": 10,
        # Delay (range) axis limits, in units of range bins (~spacing_range meters each).
        # Kept tight around zero delay so that short-range targets / small time differences
        # are clearly resolved instead of being squashed into a wide, mostly-empty axis.
        "delay_min": -1,
        "delay_max": 3,
        "doppler_oversampling": 3,
        "doppler_range": 20,
    }

    def __init__(self, argv):
        super().__init__(argv)

        # Current RX data format and its subcarrier count (updated adaptively as CSI arrives).
        self.rx_format = "lltf"
        self.subcarriers = SUBCARRIERS

        self.raw_csi_buffer_head = 0
        self.most_recent_index = 0
        self.most_recent_timestamp = -1
        self.packet_count = 0
        self.last_packet_count = 0
        self.packets_s = 0
        self.data = np.zeros((10, 10, 4), dtype=np.float64)
        self.colormap = colormaps.get("viridis")
        self.mutex = threading.Lock()

        self._reset_buffers_locked(self.subcarriers)

        # Fallback fixed gains, only used if AGC gain calibration collects no packets
        self.gain_settings = {
            "fft_scale_enable": True,
            "fft_scale_value": 20,
            "rx_gain_enable": True,
            "rx_gain_value": 33,
        }

        self.init_radar_control()
        self.rx_active = False

        self.initialize_pool()

        self.engine.rootContext().setContextProperty("backend", self)
        self.initialize_qml(pathlib.Path(__file__).parent / "doppler-delay-radar-ui.qml")

    def onAboutToQuit(self):
        try:
            self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})
        except Exception:
            pass
        super().onAboutToQuit()

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        # Apply the RX acquire config for the current preamble-format selection (default "auto"
        # accepts every LTF format, so any TX format is received and auto-detected).
        self.apply_rx_acquire_config()
        self.refresh_radar_options()
        # Only process packets fully received by the selected RX array (works for bistatic setups
        # where the transmitter is on a different array, so the whole pool is never complete).
        self.pool.add_csi_callback(
            self.data_callback,
            cb_predicate=self.radar_rx_predicate,
        )
        self.start()
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)
        # Calibration may leave the gains locked; reset to AGC so startup is AGC by default.
        self.reset_radar_gains()

    def start(self):
        self.thread = threading.Thread(target=self.__run, daemon=True)
        self.thread.start()

    def __run(self):
        while True:
            self.pool.run()

    # ---- RadarControlMixin hooks ----
    def radar_gain_fallback(self):
        return self.gain_settings

    def on_radar_active(self, active):
        # Gate DSP processing and drop stale data on every radar (re)start / stop.
        self.rx_active = active
        self.clear_data()

    def _on_update_app_state(self, newcfg):
        if "period_ms" in newcfg:
            self.axisScaleChanged.emit()
        if "rx_array" in newcfg:
            self.clear_data()  # drop stale CSI from the previously selected receiver array
        super()._on_update_app_state(newcfg)

    # ---- axis scaling (depends on RX format + TX interval) ----
    @QtCore.pyqtProperty(int, notify=axisScaleChanged)
    def num_tx_packets(self):
        return int(1 / (self.appconfig.get("period_ms") * 1e-3))

    @QtCore.pyqtProperty(int, constant=False, notify=statusUpdate)
    def packets_per_second(self):
        return self.packets_s

    @QtCore.pyqtProperty(int, constant=True)
    def delay_min(self):
        return self.appconfig.get("delay_min")

    @QtCore.pyqtProperty(int, constant=True)
    def delay_max(self):
        return self.appconfig.get("delay_max")

    @QtCore.pyqtProperty(int, constant=True)
    def doppler_range(self):
        return self.appconfig.get("doppler_range")

    @QtCore.pyqtProperty(float, notify=axisScaleChanged)
    def spacing_doppler(self):
        return SPEED_OF_LIGHT / CARRIER_FREQUENCY / (self.appconfig.get("period_ms") * 1e-3 * NUM_DOPPLER_PACKETS)

    @QtCore.pyqtProperty(float, notify=axisScaleChanged)
    def spacing_range(self):
        bandwidth = self.subcarriers * espargos.radar.rx_subcarrier_spacing(self.rx_format)
        return 1 / bandwidth * SPEED_OF_LIGHT / 2

    @QtCore.pyqtProperty(str, notify=axisScaleChanged)
    def rx_format_label(self):
        return self.rx_format.upper()

    @QtCore.pyqtSlot()
    def update_data(self):
        self.update_data_delay_doppler()

    def _reset_buffers_locked(self, subcarriers):
        """Allocate/clear all CSI buffers for ``subcarriers`` subcarriers. Caller holds the mutex."""
        self.subcarriers = subcarriers
        self.raw_csi_buffer_head = 0
        self.raw_csi_buffer = np.zeros((BUFFER_LENGTH, 2, 4, subcarriers), dtype=np.complex64)
        self.csi_buffer = np.zeros((BUFFER_LENGTH, 2, 4, subcarriers), dtype=np.complex64)
        self.mean = np.zeros((2, 4, subcarriers), dtype=np.complex64)
        self.sto_correction = np.ones(subcarriers, dtype=np.complex64)
        self.most_recent_index = 0
        self.most_recent_timestamp = -1
        self.data = np.zeros((10, 10, 4), dtype=np.float64)
        self.packet_count = 0
        self.last_packet_count = 0
        self.packets_s = 0

    @QtCore.pyqtSlot()
    def clear_data(self):
        with self.mutex:
            self._reset_buffers_locked(self.subcarriers)

    @QtCore.pyqtSlot()
    def update_status(self):
        with self.mutex:
            packets = self.packet_count
        self.packets_s = packets - self.last_packet_count
        self.last_packet_count = packets
        self.statusUpdate.emit()

    @QtCore.pyqtSlot()
    def calculate_clutter(self):
        with self.mutex:
            if self.packet_count == 0:
                return

            n = self.subcarriers
            self.mean = np.sum(self.raw_csi_buffer, axis=0) / min(self.raw_csi_buffer.shape[0], self.packet_count)

            data = self.raw_csi_buffer[0 : np.min([self.raw_csi_buffer.shape[0], self.packet_count]), ...].reshape(-1, 8, n)
            tx_antenna = self.appconfig.get("tx_antenna")
            per_board = espargos.constants.ANTENNAS_PER_BOARD
            # data holds the selected RX array's 8 sensors. Remove the transmitting sensor only when
            # it is on that same array (monostatic self-TX); in a bistatic setup the TX sensor is on a
            # different array and is not present here (tx_antenna is a pool-wide index, so it must be
            # mapped to a local 0..7 index and skipped entirely when it belongs to another array).
            if isinstance(tx_antenna, int) and tx_antenna // per_board == self.rx_boards():
                data = np.delete(data, tx_antenna % per_board, axis=1)

            correlation = np.mean(np.einsum("tai,taj->taij", data, data.conj()), axis=(0, 1))

            w, v = np.linalg.eigh(correlation)
            v_sorted = v[:, np.argsort(np.abs(w))[::-1]]

            n_signal = 1
            noise_space = v_sorted[:, n_signal:]

            exponents = np.arange(-(n - 1), n)
            coefficients = np.zeros((noise_space.shape[1], exponents.shape[0]), dtype=np.complex128)

            for exponent in exponents:
                for i in range(noise_space.shape[0]):
                    j = i - exponent
                    if 0 <= j < noise_space.shape[0]:
                        coefficients[:, exponent + (n - 1)] += noise_space[i, :].conj() * noise_space[j, :]

            coefficients = np.sum(coefficients, axis=0)
            roots = np.roots(coefficients)
            roots = roots[np.abs(roots) < 1.0]
            roots = roots[np.argsort(np.abs(np.abs(roots) - 1.0))]
            offset = np.angle(roots[:n_signal])
            # A degenerate correlation (can happen bistatically) may leave no in-unit-circle root:
            # skip STO estimation rather than crash / produce a mismatched-shape correction.
            if offset.size < n_signal or not np.all(np.isfinite(offset)):
                self.sto_correction = np.ones(n, dtype=np.complex64)
            else:
                self.sto_correction = np.exp(1j * offset * np.arange(-(n // 2), n // 2 + 1))

            self.mean = self.mean * self.sto_correction[None, None, :]

    def data_callback(self, csi):
        if not self.ready:
            return
        # Ignore packets while gain calibration / settling is in progress
        if not self.rx_active:
            return
        with self.mutex:
            if not csi.has_radar_tx_report():
                return

            calibration = self.pool.get_calibration()
            resolved_format, csi_values = espargos.radar.deserialize_rx_csi(csi, calibration, self.genericconfig.get("preamble_format"))
            if csi_values is None:
                return

            # Adapt the DSP to a changed RX format (auto-detection or user selection): re-size
            # all buffers to the new subcarrier count, drop stale data, and skip this packet.
            if resolved_format != self.rx_format:
                self.rx_format = resolved_format
                self._reset_buffers_locked(csi_values.shape[-1])
                self.axisScaleChanged.emit()
                return

            # Process only the selected receiver array (bistatic: TX array != RX array)
            rx_board = self.rx_boards()
            if rx_board >= csi_values.shape[0]:
                return
            csi_rx = csi_values[rx_board, ...]

            tx_info = csi.get_radar_tx_info()
            tx_timestamp_s = tx_info.get_hardware_tx_timestamp_ns() * 1e-9
            tx_index = csi.get_radar_tx_index()

            index = 0
            time_spacing = self.appconfig.get("period_ms") * 1e-3
            timestamp = tx_timestamp_s

            if self.most_recent_timestamp == -1:
                self.most_recent_timestamp = timestamp
            else:
                index = int(np.rint((timestamp - self.most_recent_timestamp) / time_spacing)) + self.most_recent_index

            _index = index % BUFFER_LENGTH

            subcarrier_freqs = espargos.radar.rx_subcarrier_frequencies(calibration, resolved_format)

            csi_rx = espargos.radar.correct_radar_csi_tx_timestamps(
                csi_rx[np.newaxis, ...],
                np.array([tx_timestamp_s]),
                np.array([tx_index]),
                subcarrier_freqs,
                calibration.sensor_clock_offsets,
                tx_timestamp_offset_s=self.appconfig.get("tx_timestamp_offset_ns") * 1e-9,
            )[0]

            # In monostatic mode the transmitting sensor is on the RX array and never receives its own
            # packet, so its incomplete CSI is NaN — that is expected and must NOT drop the packet (the
            # DSP uses the other row). Sanitize NaN/inf to zero instead: this also neutralizes a
            # genuinely corrupted packet (e.g. a NaN TX reference in a bistatic setup) so it can't
            # poison the clutter mean / delay-Doppler FFT.
            csi_rx = np.nan_to_num(csi_rx, nan=0.0, posinf=0.0, neginf=0.0)

            self.raw_csi_buffer[self.raw_csi_buffer_head] = csi_rx
            self.raw_csi_buffer_head = (self.raw_csi_buffer_head + 1) % BUFFER_LENGTH
            self.packet_count += 1

            self.csi_buffer[_index] = csi_rx * self.sto_correction[None, None, :] - self.mean

            if index > self.most_recent_index:
                gap = np.arange(1, index - self.most_recent_index)
                gap_index = (self.most_recent_index + gap) % BUFFER_LENGTH
                N = gap_index.shape[0]
                if N > 0:
                    _last_index = self.most_recent_index % BUFFER_LENGTH
                    left_amp = np.abs(self.csi_buffer[_last_index, ...])
                    right_amp = np.abs(self.csi_buffer[_index, ...])
                    amp_diff = right_amp - left_amp
                    phase_diff = np.angle(self.csi_buffer[_index, ...] * np.conj(self.csi_buffer[_last_index, ...]))
                    amp = left_amp + gap[:, None, None, None] * amp_diff[None, :, :, :] / (N + 1)
                    phase = gap[:, None, None, None] * phase_diff[None, :, :, :] / (N + 1) + np.angle(self.csi_buffer[_last_index, ...])
                    self.csi_buffer[gap_index, ...] = amp * np.exp(1j * phase)

                self.most_recent_index = index
                self.most_recent_timestamp = timestamp

    def update_data_delay_doppler(self):
        num_packets = NUM_DOPPLER_PACKETS
        self.mutex.acquire()
        subcarriers = self.subcarriers
        _index = self.most_recent_index % BUFFER_LENGTH
        csi = np.roll(self.csi_buffer[:, 1, :, :], -(_index - num_packets + 1), axis=0)[:num_packets, ...]
        csi = np.sum(csi, axis=1)
        self.mutex.release()

        doppler_os = self.appconfig.get("doppler_oversampling")
        delay_os = self.appconfig.get("delay_oversampling")

        delay_min = self.appconfig.get("delay_min")
        delay_max = self.appconfig.get("delay_max")

        padded_csi = np.zeros((num_packets * doppler_os, subcarriers * delay_os), dtype=np.complex64)
        padded_csi[0:num_packets, 0:subcarriers] = csi
        doppler_delay = np.abs(scipy.fft.fft(scipy.fft.ifft(padded_csi, axis=1), axis=0))

        n = doppler_delay.size
        k = int(0.99 * n)
        scale = (np.partition(doppler_delay.ravel(), k)[k] + 1) * 10

        doppler_delay = np.roll(np.roll(doppler_delay, -delay_min * delay_os, axis=1), doppler_delay.shape[0] // 2, axis=0)
        doppler_delay = doppler_delay[:, 0 : ((delay_max - delay_min) * delay_os + 1)]
        doppler_delay = doppler_delay / (scale if np.max(doppler_delay) < scale else np.max(doppler_delay))

        doppler_range = self.appconfig.get("doppler_range")
        index_range = doppler_range * doppler_os
        doppler_delay = doppler_delay[doppler_delay.shape[0] // 2 - index_range : doppler_delay.shape[0] // 2 + index_range + 1, ...]

        self.data = self.colormap(doppler_delay.T)

        self.dataChanged.emit()


QtQml.qmlRegisterType(QImageTexture, "Custom", 1, 0, "QImageTexture")
app = DopplerDelayApp(sys.argv)

sys.exit(app.exec())
