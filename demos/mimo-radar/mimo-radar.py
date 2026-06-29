# Front view (left to right): board1 | board0 | board2
import espargos
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

SUBCARRIERS = 53
BUFFER_LENGTH = 300
N_antennas = 24
TX_TIME_SPACING = 2500

RF_SWITCH = 2
RX_GAIN = 25
RX_FFT_SCALE = 15

TX_POWER = 60


RX_config = {"fft_scale_enable": True, "fft_scale_value": RX_FFT_SCALE, "rx_gain_enable": True, "rx_gain_value": RX_GAIN}

SENSORS_ACTIVE = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
TDM_TIMESLOTS = [0, -1, -1, -1, 1, -1, -1, -1, 2, -1, -1, -1, 3, -1, -1, -1, 4, -1, -1, -1, 5, -1, -1, -1]


def unpack(x, n_antennas):
    a_real = np.concatenate([[1.0], x[: n_antennas - 1]])
    a_imag = np.concatenate([[0.0], x[n_antennas - 1 : 2 * (n_antennas - 1)]])
    t = np.concatenate([[0.0], x[2 * (n_antennas - 1) :]])
    a = a_real + 1j * a_imag
    return a, t


def pack(a, t):
    a_real = np.real(a)
    a_imag = np.imag(a)
    return np.concatenate((a_real[1:], a_imag[1:], t[1:]))


def split_residuals(r_complex: np.ndarray) -> np.ndarray:
    return np.concatenate([r_complex.real, r_complex.imag])


def split_jacobian(J_da, J_da_conj, J_dt) -> np.ndarray:
    real_r_real_a = np.real(J_da + J_da_conj)
    real_r_imag_a = -np.imag(J_da - J_da_conj)
    imag_r_real_a = np.imag(J_da + J_da_conj)
    imag_r_imag_a = np.real(J_da - J_da_conj)
    real_r_dt = np.real(J_dt)
    imag_r_dt = np.imag(J_dt)

    return np.block([[real_r_real_a, real_r_imag_a, real_r_dt], [imag_r_real_a, imag_r_imag_a, imag_r_dt]])


def calculate_residuals(x, n_antennas, csi):
    a, t = unpack(x, n_antennas)
    assert csi.shape[0] == n_antennas
    assert csi.shape[1] == n_antennas
    subcarriers_freq = ((np.arange(csi.shape[2]) - csi.shape[2] // 2) * 312.5e3) * 1e-9
    r_complex = np.zeros((n_antennas, n_antennas, csi.shape[2]), dtype=np.complex128)
    for tx in range(n_antennas):
        for rx in range(n_antennas):
            if tx == rx:
                continue
            else:
                r_complex[tx, rx, :] = csi[tx, rx, :] - a[tx] * (csi[tx, rx, :] * a[tx].conj() * np.exp(1j * 2 * np.pi * subcarriers_freq * t[tx]) + csi[rx, tx, :] * a[rx].conj() * np.exp(1j * 2 * np.pi * subcarriers_freq * t[rx])) / (
                    a[tx] * a[tx].conj() + a[rx] * a[rx].conj()
                ) * np.exp(-1j * 2 * np.pi * subcarriers_freq * t[tx])
    return split_residuals(r_complex.flatten())


def calculate_jacobian(x, n_antennas, csi):
    a, t = unpack(x, n_antennas)
    assert csi.shape[0] == n_antennas
    assert csi.shape[1] == n_antennas
    subcarriers_freq = ((np.arange(csi.shape[2]) - csi.shape[2] // 2) * 312.5e3) * 1e-9
    J_da = np.zeros((n_antennas, n_antennas, csi.shape[2], n_antennas), dtype=np.complex128)
    J_da_conj = np.zeros((n_antennas, n_antennas, csi.shape[2], n_antennas), dtype=np.complex128)
    J_dt = np.zeros((n_antennas, n_antennas, csi.shape[2], n_antennas), dtype=np.complex128)
    for tx in range(n_antennas):
        for rx in range(n_antennas):
            if tx == rx:
                continue
            else:
                C = csi[tx, rx, :] * a[tx].conj() * np.exp(1j * 2 * np.pi * subcarriers_freq * t[tx]) + csi[rx, tx, :] * a[rx].conj() * np.exp(1j * 2 * np.pi * subcarriers_freq * t[rx])
                D = a[tx] * a[tx].conj() + a[rx] * a[rx].conj()
                for index in range(n_antennas):
                    if index == tx:
                        J_da[tx, rx, :, index] = -np.exp(-1j * 2 * np.pi * subcarriers_freq * t[tx]) * (a[rx] * a[rx].conj() * C) / (D**2)
                        J_da_conj[tx, rx, :, index] = -np.exp(-1j * 2 * np.pi * subcarriers_freq * t[tx]) * (a[tx] * csi[tx, rx, :] * np.exp(1j * 2 * np.pi * subcarriers_freq * t[tx]) * D - a[tx] * a[tx] * (C)) / (D**2)
                    elif index == rx:
                        J_da[tx, rx, :, index] = np.exp(-1j * 2 * np.pi * subcarriers_freq * t[tx]) * (a[tx] * a[rx].conj() * C) / (D**2)
                        J_da_conj[tx, rx, :, index] = -np.exp(-1j * 2 * np.pi * subcarriers_freq * t[tx]) * (a[tx] * csi[rx, tx, :] * np.exp(1j * 2 * np.pi * subcarriers_freq * t[rx]) * D - a[tx] * a[rx] * C) / (D**2)
                    else:
                        J_da[tx, rx, :, index] = 0
                        J_da_conj[tx, rx, :, index] = 0
                    if index == tx:
                        J_dt[tx, rx, :, index] = a[tx] / D * (1j * 2 * np.pi * subcarriers_freq * csi[rx, tx, :] * a[rx].conj() * np.exp(1j * 2 * np.pi * subcarriers_freq * (t[rx] - t[tx])))
                    elif index == rx:
                        J_dt[tx, rx, :, index] = -a[tx] / D * (1j * 2 * np.pi * subcarriers_freq * csi[rx, tx, :] * a[rx].conj() * np.exp(1j * 2 * np.pi * subcarriers_freq * (t[rx] - t[tx])))
                    else:
                        J_dt[tx, rx, :, index] = 0
    J_full = split_jacobian(J_da.reshape((-1, n_antennas)), J_da_conj.reshape((-1, n_antennas)), J_dt.reshape((-1, n_antennas)))
    fixed_columns = [0, n_antennas, 2 * (n_antennas)]
    return np.delete(J_full, fixed_columns, axis=1)


def calculate_initialization(csi):
    subcarriers_freq = ((np.arange(csi.shape[2]) - csi.shape[2] // 2) * 312.5e3) * 1e-9
    t_init = np.angle(np.sum((csi[0, :, 1:] / csi[:, 0, 1:]) * (csi[0, :, :-1] / csi[:, 0, :-1]).conj(), axis=1)) / (2 * np.pi * 312.5e3 * 1e-9)
    a_init = 1 / csi.shape[2] * np.sum((csi[:, 0, :] / csi[0, :, :]) * np.exp(1j * 2 * np.pi * subcarriers_freq * t_init[:, np.newaxis]), axis=1)
    t_init[0] = 0
    a_init[0] = 1
    return a_init, t_init


def least_squares_optimization(csi):
    n_antennas = csi.shape[0]
    a_init, t_init = calculate_initialization(csi)
    x0 = pack(a_init, t_init)
    result = scipy.optimize.least_squares(calculate_residuals, x0, jac=calculate_jacobian, args=(n_antennas, csi), method="lm")
    a_est, t_est = unpack(result.x, n_antennas)
    return a_est, t_est


class QImageTexture(QQuickPaintedItem):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidth(10)
        self.setHeight(10)
        self.colormap = colormaps.get_cmap("viridis")
        texture_data = np.transpose(np.zeros((10, 10, 4)))
        self.image_data = np.ascontiguousarray((texture_data * 255).astype(np.uint8))
        self.image = QImage(self.image_data.data, 10, 10, 4 * 10, QImage.Format.Format_RGBA8888)

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


class MIMOApplication(ESPARGOSApplication):
    dataChanged = QtCore.pyqtSignal()
    configChanged = QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "display_mode": "Azimuth_Delay",
        "delay_min": -3,
        "delay_max": 5,
    }

    def _on_update_app_state(self, newconfig):
        if "delay_min" in newconfig or "delay_max" in newconfig:
            self.configChanged.emit()
        super()._on_update_app_state(newconfig)

    def __init__(self, argv):
        super().__init__(argv)

        self.explicit_initial_config.setdefault("pool", {})["acquire_lltf_force"] = True

        self.data = np.zeros((10, 10))

        self.initialize_pool(calibrate=True)

        self.clutter = np.zeros((N_antennas, N_antennas, SUBCARRIERS), dtype=np.complex64)

        self.csi_buffer = np.zeros((BUFFER_LENGTH, N_antennas, N_antennas, SUBCARRIERS), dtype=np.complex64)
        self.csi_buffer_nc = np.zeros((BUFFER_LENGTH, N_antennas, N_antennas, SUBCARRIERS), dtype=np.complex64)
        self.csi_buffer[...] = np.nan
        self.csi_buffer_nc[...] = np.nan

        self.m_r_index = 0
        self.m_r_timestamp = -1
        self.last_index_tx = np.zeros((N_antennas), dtype=np.int64)

        self.tx_correction = np.ones((N_antennas, SUBCARRIERS), dtype=np.complex64)
        self.sto_correction = np.ones(SUBCARRIERS, dtype=np.complex64)

        self.mutex = threading.Lock()

        self.engine.rootContext().setContextProperty("backend", self)
        self.initialize_qml("mimo-radar-ui.qml")
        self.colormap = colormaps.get_cmap("viridis")

    def _csi_predicate(self, cluster):
        return cluster.is_radar() and cluster.has_radar_tx_report() and np.sum(cluster.get_completion()) == N_antennas - 1

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)
        self.pool.add_csi_callback(self.onCSI, cb_predicate=self._csi_predicate)
        self.start()
        self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})
        self.pool.set_gain_settings(RX_config)
        self.pool.set_rfswitch(RF_SWITCH)
        self.pool.set_csi_acquire_config(config={"acquire_csi_force_lltf": True})

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            self.pool.run()

    def _subcarrier_frequencies(self, channel) -> np.ndarray:
        frequencies = espargos.util.get_frequencies_lltf(channel)
        center = espargos.util.get_center_frequency(channel)
        return frequencies - center

    def onCSI(self, csi):
        radar_csi = espargos.radar.correct_radar_csi_tx_timestamps(
            self.pool.get_calibration().apply_lltf(csi.deserialize_csi_lltf()).reshape((-1, SUBCARRIERS)),
            csi.get_radar_tx_info().get_hardware_tx_timestamp_ns() / 1e9,
            csi.get_radar_tx_index(),
            self._subcarrier_frequencies(csi.get_primary_channel()),
            np.zeros((N_antennas,), dtype=np.float64),
            tx_timestamp_offset_s=1075e-9,
        )
        tx_index = csi.get_radar_tx_index()

        timestamp = csi.get_radar_tx_info().get_hardware_tx_timestamp_ns() / 1e9 - self.pool.get_calibration().sensor_clock_offsets.reshape(-1)[tx_index]
        _timestamp = timestamp - np.asarray(TDM_TIMESLOTS)[tx_index] * TX_TIME_SPACING * 1e-6

        index = 0

        with self.mutex:
            if self.m_r_timestamp == -1:
                self.m_r_timestamp = _timestamp
            else:
                index = int(np.rint((_timestamp - self.m_r_timestamp) / (6 * TX_TIME_SPACING * 1e-6))) + self.m_r_index

            _index = index % BUFFER_LENGTH

            self.csi_buffer[_index, tx_index, :, :] = radar_csi
            self.csi_buffer_nc[_index, tx_index, :, :] = (radar_csi - self.clutter[tx_index, :, :]) * self.tx_correction[tx_index, None, :] * self.sto_correction[None, :]

            if index > self.last_index_tx[tx_index]:
                gap = np.arange(1, index - self.last_index_tx[tx_index])
                gap_index = (gap + self.last_index_tx[tx_index]) % BUFFER_LENGTH
                if gap_index.shape[0] > 0:
                    self.csi_buffer[gap_index, tx_index, ...] = np.nan
                    self.csi_buffer_nc[gap_index, tx_index, ...] = np.nan
                self.last_index_tx[tx_index] = index
                if index > self.m_r_index:
                    self.m_r_index = index
                    self.m_r_timestamp = _timestamp

    def calculate_tx_and_delay_correction(self):
        with self.mutex:
            mask = np.asarray(SENSORS_ACTIVE, dtype=bool)
            csi = np.nanmean(self.csi_buffer, axis=0)[mask, ...][:, mask, ...]
            a, t = least_squares_optimization(csi)
            subcarriers_freq = ((np.arange(SUBCARRIERS) - SUBCARRIERS // 2) * 312.5e3) * 1e-9
            tx_correction = 1 / a[:, None] * np.exp(1j * 2 * np.pi * subcarriers_freq[None, :] * t[:, None])
            self.tx_correction[mask, :] = tx_correction

            corrected_csi = self.csi_buffer * self.tx_correction[None, :, None, :]
            idx = np.array([[12, 13], [12, 8], [8, 9], [8, 12], [8, 4], [4, 5], [4, 8], [4, 0], [0, 1], [0, 4], [0, 20], [20, 21], [20, 0], [20, 16], [16, 17], [16, 20]])
            data = corrected_csi[:, idx[:, 0], idx[:, 1], :]
            correlation = np.nanmean(np.einsum("tai,taj->taij", data, data.conj()), axis=(0, 1))

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

    def calculate_clutter(self):
        with self.mutex:
            mean = np.nanmean(self.csi_buffer, axis=0)
            self.clutter[...] = mean

    def start_radar(self):
        if self.pool.get_calibration() is None:
            raise RuntimeError("Calibration data not available")
        calibration = self.pool.get_calibration()
        active_by_sensor = np.array(SENSORS_ACTIVE, dtype=bool).reshape(3, 2, 4).tolist()
        t0_by_sensor = (np.array(TDM_TIMESLOTS) * TX_TIME_SPACING * 1e-6 + 1e-3 - float(np.nanmin(self.pool.get_calibration().sensor_clock_offsets))).reshape(3, 2, 4).tolist()
        period_by_sensor = np.full((3, 2, 4), TX_TIME_SPACING * 1e-6 * 6).tolist()

        tx_power = TX_POWER
        tx_rate = 11
        rf_switch_state = RF_SWITCH
        tx_phymode = 2

        radarConfig = espargos.radar.build_pool_config(
            calibration=calibration, active_by_sensor=active_by_sensor, t0_by_sensor=t0_by_sensor, period_by_sensor=period_by_sensor, tx_power=tx_power, tx_rate=tx_rate, rfswitch_state=rf_switch_state, tx_phymode=tx_phymode
        )

        self.csi_buffer[...] = np.nan
        self.csi_buffer_nc[...] = np.nan
        self.m_r_index = 0
        self.m_r_timestamp = -1
        self.last_index_tx = np.zeros((N_antennas), dtype=np.int64)

        self.pool.set_radar_config(radarConfig)

    def stop_radar(self):
        self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})

    def clear_data(self):
        with self.mutex:
            self.csi_buffer[...] = np.nan
            self.csi_buffer_nc[...] = np.nan
            self.m_r_index = 0
            self.m_r_timestamp = -1
            self.last_index_tx = np.zeros((N_antennas), dtype=np.int64)

    def azimuth_delay(self):
        num_frames = 30
        with self.mutex:
            tx_mask = np.asarray(SENSORS_ACTIVE, dtype=bool)
            end_idx = int(np.min(self.last_index_tx[tx_mask]))
            idx = np.arange(end_idx - num_frames + 1, end_idx + 1, dtype=np.int64) % BUFFER_LENGTH
            csi = np.nan_to_num(self.csi_buffer_nc[idx, ...])
        delay_min = self.appconfig.get("delay_min")
        delay_max = self.appconfig.get("delay_max")
        tx_indices = np.array([12, 8, 4, 0, 20, 16])
        rx_indices = np.array([[13, 9, 5, 1, 21, 17], [14, 10, 6, 2, 22, 18], [15, 11, 7, 3, 23, 19]])
        csi = csi[
            :,
            tx_indices[:, None, None],
            rx_indices[None, :, :],
            :,
        ]
        csi = np.sum(csi, axis=2)
        beamspace_os = 10
        delay_os = 10
        doppler_csi = scipy.fft.fft(csi, axis=0)
        index_tx_axis = np.asarray(TDM_TIMESLOTS)[tx_indices]
        index_doppler_axis = np.fft.fftfreq(doppler_csi.shape[0]) * doppler_csi.shape[0]
        phase_correction = np.exp(-2j * np.pi * 1 / doppler_csi.shape[0] * index_doppler_axis[:, None] * (index_tx_axis[None, :] / doppler_csi.shape[1]))[:, :, None, None]
        doppler_csi = doppler_csi * phase_correction
        csi_combined = np.zeros((doppler_csi.shape[0], doppler_csi.shape[1], 2 * doppler_csi.shape[1] - 1, doppler_csi.shape[3]), dtype=np.complex64)
        for tx in range(doppler_csi.shape[1]):
            csi_combined[:, tx, tx : (tx + doppler_csi.shape[1]), :] = doppler_csi[:, tx, :, :]
        csi_combined = np.sum(csi_combined, axis=1)
        # csi_combined = np.sum(csi_combined, axis=1)/np.array([1,2,3,4,5,6,5,4,3,2,1])[None,:,None] #remove implicit bartlett windowing
        padded_csi = np.zeros((csi_combined.shape[0], csi_combined.shape[1] * beamspace_os, csi_combined.shape[2] * delay_os), dtype=np.complex64)
        padded_csi[:, 0 : csi_combined.shape[1], 0 : csi_combined.shape[2]] = csi_combined
        dbd_csi = np.abs(scipy.fft.fft(scipy.fft.ifft(padded_csi, axis=-1), axis=-2))
        dbd_csi = np.roll(np.roll(dbd_csi, -delay_min * delay_os, axis=-1), dbd_csi.shape[-2] // 2, axis=-2)
        dbd_csi = np.append(dbd_csi, dbd_csi[:, 0:1, :], axis=-2)
        dbd_csi = dbd_csi[:, :, 0 : (delay_max - delay_min) * delay_os]
        k = int(0.99 * dbd_csi.size)
        scale = (np.partition(dbd_csi.ravel(), k)[k] + 1) * 5
        dbd_csi = dbd_csi / (scale if np.max(dbd_csi) < scale else np.max(dbd_csi))

        bd_csi = np.max(dbd_csi, axis=0)
        self.data = self.colormap(bd_csi.T)

    def onAboutToQuit(self):
        if hasattr(self, "pool"):
            self.pool.set_radar_config({"active_by_antid": [False] * espargos.constants.ANTENNAS_PER_BOARD})
        return super().onAboutToQuit()

    @QtCore.pyqtSlot()
    def update_data(self):
        self.azimuth_delay()
        self.dataChanged.emit()

    @QtCore.pyqtSlot()
    def start_radar_slot(self):
        self.start_radar()

    @QtCore.pyqtSlot()
    def stop_radar_slot(self):
        self.stop_radar()

    @QtCore.pyqtSlot()
    def calculate_tx_and_delay_correction_slot(self):
        self.calculate_tx_and_delay_correction()

    @QtCore.pyqtSlot()
    def calculate_clutter_slot(self):
        self.calculate_clutter()

    @QtCore.pyqtSlot()
    def clear_data_slot(self):
        self.clear_data()

    @QtCore.pyqtProperty(int, constant=False, notify=configChanged)
    def delay_min(self):
        return self.appconfig.get("delay_min")

    @QtCore.pyqtProperty(int, constant=False, notify=configChanged)
    def delay_max(self):
        return self.appconfig.get("delay_max")


QtQml.qmlRegisterType(QImageTexture, "Custom", 1, 0, "QImageTexture")
app = MIMOApplication(sys.argv)

sys.exit(app.exec())
