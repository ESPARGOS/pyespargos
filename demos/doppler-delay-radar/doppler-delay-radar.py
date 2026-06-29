import espargos
import espargos.constants
import espargos.radar
import espargos.util
import numpy as np
import scipy
from PyQt6 import QtCore, QtQml, QtWidgets
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtQuick import QQuickPaintedItem
from matplotlib import colormaps
import pathlib
import sys
import threading

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))
from demos.common import ESPARGOSApplication

BUFFER_LENGTH = 1000
SUBCARRIERS = 53

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


class DopplerDelayApp(ESPARGOSApplication):
    dataChanged = QtCore.pyqtSignal()
    statusUpdate = QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "delay_oversampling": 10,
        "delay_min": -3,
        "delay_max": 5,
        "doppler_oversampling": 3,
        "doppler_range": 20,
        "packet_interval_us": 4882,
        "tx_power": 34,
        "tx_rf_switch": 2,
        "tx_antenna": 3,
        "tx_timestamp_offset_ns": 1085,
    }

    def __init__(self, argv):
        super().__init__(argv)

        self.explicit_initial_config.setdefault("pool", {})["acquire_lltf_force"] = True

        self.raw_csi_buffer_head = 0
        self.raw_csi_buffer = np.zeros((BUFFER_LENGTH, 2, 4, SUBCARRIERS), dtype=np.complex64)

        self.csi_buffer = np.zeros((BUFFER_LENGTH, 2, 4, SUBCARRIERS), dtype=np.complex64)
        self.m_r_index = 0
        self.m_r_timestamp = -1

        self.mean = np.zeros((2, 4, SUBCARRIERS), dtype=np.complex64)

        self.packet_count = 0
        self.last_packet_count = 0
        self.packets_s = 0

        self.data = np.zeros((10, 10, 4), dtype=np.float64)
        self.colormap = colormaps.get("viridis")

        self.mutex = threading.Lock()

        self.sto_correction = np.ones(SUBCARRIERS, dtype=np.complex64)

        self.gain_settings = {
            "fft_scale_enable": True,
            "fft_scale_value": 20,
            "rx_gain_enable": True,
            "rx_gain_value": 33,
        }

        self.initialize_pool()

        self.engine.rootContext().setContextProperty("backend", self)
        self.initialize_qml(pathlib.Path(__file__).parent / "doppler-delay-radar-ui.qml")

    def onAboutToQuit(self):
        try:
            self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})
        except Exception:
            pass
        super().onAboutToQuit()


    def _csi_predicate(self,cluster):
        return cluster.is_radar() and cluster.has_radar_tx_report() and np.sum(cluster.get_completion()) == 7
    
    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        self.pool.set_gain_settings(self.gain_settings)
        self.pool.add_csi_callback(
            self.data_callback,
            cb_predicate=self._csi_predicate,
        )
        self.start()
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)

    def start(self):
        self.thread = threading.Thread(target=self.__run, daemon=True)
        self.thread.start()

    def __run(self):
        while True:
            self.pool.run()

    @QtCore.pyqtSlot()        
    def start_tx(self):
        calibration = self.pool.get_calibration()
        if calibration is None:
            return

        tx_antenna = self.appconfig.get("tx_antenna")
        packet_period_s = self.appconfig.get("packet_interval_us") * 1e-6

        active_by_sensor = [False] * espargos.constants.ANTENNAS_PER_BOARD
        active_by_sensor[tx_antenna] = True

        min_safe_start = max(0.0, - float(np.nanmin(calibration.sensor_clock_offsets))) + 1e-6
        t0_by_sensor = [min_safe_start ]* espargos.constants.ANTENNAS_PER_BOARD
        period_by_sensor = [packet_period_s] * espargos.constants.ANTENNAS_PER_BOARD

        pool_config = espargos.radar.build_pool_config(
            calibration=calibration,
            active_by_sensor=np.array(active_by_sensor).reshape(2,4).tolist(),
            t0_by_sensor=np.array(t0_by_sensor).reshape(2,4).tolist(),
            period_by_sensor=np.array(period_by_sensor).reshape(2,4).tolist(),
            tx_power=int(self.appconfig.get("tx_power")),
            tx_phymode=2,
            tx_rate=11,
            rfswitch_state=int(self.appconfig.get("tx_rf_switch")),
        )
        self.pool.set_radar_config(pool_config)

    @QtCore.pyqtSlot()
    def stop_radar(self):
        self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})

    @QtCore.pyqtProperty(int, constant=True)
    def num_tx_packets(self):
        return int(1 / (self.appconfig.get("packet_interval_us") * 1e-6))

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

    @QtCore.pyqtProperty(float, constant=True)
    def spacing_doppler(self):
        spacing = SPEED_OF_LIGHT / CARRIER_FREQUENCY * 1 / (self.appconfig.get("packet_interval_us") * 1e-6 * 101)
        return spacing

    @QtCore.pyqtProperty(float, constant=True)
    def spacing_range(self):
        spacing = 1 / (SUBCARRIERS * 312.5e3) * SPEED_OF_LIGHT / 2
        return spacing

    @QtCore.pyqtSlot()
    def update_data(self):
        self.update_data_delay_doppler()

    @QtCore.pyqtSlot()
    def clear_data(self):
        with self.mutex:
            self.raw_csi_buffer_head = 0
            self.raw_csi_buffer = np.zeros((BUFFER_LENGTH, 2, 4, SUBCARRIERS), dtype=np.complex64)
            self.csi_buffer = np.zeros((BUFFER_LENGTH, 2, 4, SUBCARRIERS), dtype=np.complex64)
            self.m_r_index = 0
            self.m_r_timestamp = -1
            self.data = np.zeros((10, 10, 4), dtype=np.float64)
            self.mean = np.zeros((2, 4, SUBCARRIERS), dtype=np.complex64)
            self.packet_count = 0
            self.last_packet_count = 0
            self.packets_s = 0

    @QtCore.pyqtSlot()
    def update_status(self):
        self.mutex.acquire()
        packets = self.packet_count
        self.mutex.release()
        self.packets_s = packets - self.last_packet_count
        self.last_packet_count = packets
        self.statusUpdate.emit()

    @QtCore.pyqtSlot()
    def calculate_clutter(self):
        with self.mutex:
            if self.packet_count == 0:
                return
            
            self.mean = np.sum(self.raw_csi_buffer, axis=0) / min(self.raw_csi_buffer.shape[0], self.packet_count)

            data = self.raw_csi_buffer[0:np.min([self.raw_csi_buffer.shape[0], self.packet_count]), ...].reshape(-1, 8, 53)
            data = np.delete(data, self.appconfig.get("tx_antenna"), axis=1)

            correlation = np.mean(np.einsum('tai,taj->taij', data, data.conj()), axis=(0, 1))

            w, v = np.linalg.eigh(correlation)
            v_sorted = v[:, np.argsort(np.abs(w))[::-1]]

            n_signal = 1
            noise_space = v_sorted[:, n_signal:]

            exponents = np.arange(-52, 53)
            coefficients = np.zeros((noise_space.shape[1], exponents.shape[0]), dtype=np.complex128)

            for exponent in exponents:
                for i in range(noise_space.shape[0]):
                    j = i - exponent
                    if 0 <= j < noise_space.shape[0]:
                        coefficients[:, exponent + 52] += noise_space[i, :].conj() * noise_space[j, :]

            coefficients = np.sum(coefficients, axis=0)
            roots = np.roots(coefficients)
            roots = roots[np.abs(roots) < 1.0]
            roots = roots[np.argsort(np.abs(np.abs(roots) - 1.0))]
            offset = np.angle(roots[:n_signal])
            self.sto_correction = np.exp(1j * offset * np.arange(-26, 27))

            self.mean = self.mean * self.sto_correction[None, None, :]

    def data_callback(self, csi):
        if not self.ready:
            return
        with self.mutex:
            if not csi.has_lltf():
                return
            if not csi.has_radar_tx_report():
                return

            tx_info = csi.get_radar_tx_info()
            tx_timestamp_s = tx_info.get_hardware_tx_timestamp_ns() * 1e-9
            tx_index = csi.get_radar_tx_index()

            index = 0
            time_spacing = self.appconfig.get("packet_interval_us") * 1e-6
            timestamp = tx_timestamp_s

            if self.m_r_timestamp == -1:
                self.m_r_timestamp = timestamp
            else:
                index = int(np.rint((timestamp - self.m_r_timestamp) / time_spacing)) + self.m_r_index

            _index = index % BUFFER_LENGTH

            calibration = self.pool.get_calibration()
            csi_lltf = (calibration.apply_lltf(csi.deserialize_csi_lltf()))[0, ...]

            channel_primary = calibration.channel_primary
            subcarrier_freqs = (
                espargos.util.get_frequencies_lltf(channel_primary)
                - espargos.util.get_center_frequency(channel_primary)
            )

            csi_lltf = espargos.radar.correct_radar_csi_tx_timestamps(
                csi_lltf[np.newaxis, ...],
                np.array([tx_timestamp_s]),
                np.array([tx_index]),
                subcarrier_freqs,
                self.pool.get_calibration().sensor_clock_offsets,
                tx_timestamp_offset_s=self.appconfig.get("tx_timestamp_offset_ns") * 1e-9,
            )[0]

            self.raw_csi_buffer[self.raw_csi_buffer_head] = csi_lltf
            self.raw_csi_buffer_head = (self.raw_csi_buffer_head + 1) % BUFFER_LENGTH
            self.packet_count += 1

            self.csi_buffer[_index] = csi_lltf * self.sto_correction[None, None, :] - self.mean

            if index > self.m_r_index:
                gap = np.arange(1, index - self.m_r_index)
                gap_index = (self.m_r_index + gap) % BUFFER_LENGTH
                N = gap_index.shape[0]
                if N > 0:
                    _last_index = self.m_r_index % BUFFER_LENGTH
                    left_amp = np.abs(self.csi_buffer[_last_index, ...])
                    right_amp = np.abs(self.csi_buffer[_index, ...])
                    amp_diff = right_amp - left_amp
                    phase_diff = np.angle(self.csi_buffer[_index, ...] * np.conj(self.csi_buffer[_last_index, ...]))
                    amp = left_amp + gap[:, None, None, None] * amp_diff[None, :, :, :] / (N + 1)
                    phase = gap[:, None, None, None] * phase_diff[None, :, :, :] / (N + 1) + np.angle(self.csi_buffer[_last_index, ...])
                    self.csi_buffer[gap_index, ...] = amp * np.exp(1j * phase)

                self.m_r_index = index
                self.m_r_timestamp = timestamp

    def update_data_delay_doppler(self):
        num_packets = 101
        self.mutex.acquire()
        _index = self.m_r_index % BUFFER_LENGTH
        csi = np.roll(self.csi_buffer[:, 1, :, :], -(_index - num_packets + 1), axis=0)[:num_packets, ...]
        csi = np.sum(csi, axis=1)
        self.mutex.release()

        doppler_os = self.appconfig.get("doppler_oversampling")
        delay_os = self.appconfig.get("delay_oversampling")

        delay_min = self.appconfig.get("delay_min")
        delay_max = self.appconfig.get("delay_max")

        padded_csi = np.zeros((num_packets * doppler_os, SUBCARRIERS * delay_os), dtype=np.complex64)
        padded_csi[0:num_packets, 0:SUBCARRIERS] = csi
        doppler_delay = np.abs(scipy.fft.fft(scipy.fft.ifft(padded_csi, axis=1), axis=0))

        n = doppler_delay.size
        k = int(0.99 * n)
        scale = (np.partition(doppler_delay.ravel(), k)[k] + 1) * 10

        doppler_delay = np.roll(np.roll(doppler_delay, -delay_min * delay_os, axis=1), doppler_delay.shape[0] // 2, axis=0)
        doppler_delay = doppler_delay[:, 0:((delay_max - delay_min) * delay_os + 1)]
        doppler_delay = doppler_delay / (scale if np.max(doppler_delay) < scale else np.max(doppler_delay))

        doppler_range = self.appconfig.get("doppler_range")
        index_range = doppler_range * doppler_os
        doppler_delay = doppler_delay[doppler_delay.shape[0] // 2 - index_range: doppler_delay.shape[0] // 2 + index_range + 1, ...]

        self.data = self.colormap(doppler_delay.T)
        self.dataChanged.emit()


QtQml.qmlRegisterType(QImageTexture, "Custom", 1, 0, "QImageTexture")
app = DopplerDelayApp(sys.argv)

sys.exit(app.exec())
