import numpy as np
import threading
import logging
import re

from . import csi

class CSIBacklog(object):
    """
    CSI backlog class. Stores CSI data in a ringbuffer for processing when needed.

    :param pool: CSI pool object to collect CSI data from
    :param enable_ht40: Enable storing CSI from HT40 frames (default: True)
    :param calibrate: Apply calibration to CSI data (default: True)
    :param cb_predicate: A function that defines the conditions under which clustered CSI is regarded as completed and thus added to the backlog.
        See :meth:`espargos.pool.Pool.add_csi_callback` for more details.
    :param size: Size of the ringbuffer (default: 100)
    """
    def __init__(self, pool, enable_lltf = True, enable_ht40 = True, calibrate = True, cb_predicate = None, size = 100):
        self.logger = logging.getLogger("pyespargos.backlog")

        self.pool = pool
        self.size = size
        self.enable_lltf = enable_lltf
        self.enable_ht40 = enable_ht40
        self.calibrate = calibrate

        self.storage_ht40 = np.zeros((size,) + self.pool.get_shape() + ((csi.csi_buf_t.htltf_lower.size + csi.HT40_GAP_SUBCARRIERS * 2 + csi.csi_buf_t.htltf_higher.size) // 2,), dtype = np.complex64)
        self.storage_lltf = np.zeros((size,) + self.pool.get_shape() + (csi.csi_buf_t.lltf.size // 2,), dtype = np.complex64)

        self.storage_timestamps = np.zeros((size,) + self.pool.get_shape(), dtype = np.float64)
        self.storage_rssi = np.zeros((size,) + self.pool.get_shape(), dtype = np.float32)
        self.storage_macs = np.zeros((size, 6), dtype = np.uint8)
        self.head = 0
        self.latest = None

        self.running = True

        def new_csi_callback(clustered_csi):
            # Check MAC address if filter is installed
            if self.mac_filter is not None:
                if not self.mac_filter.match(clustered_csi.get_source_mac()):
                    return

            # Store timestamp
            sensor_timestamps_raw = clustered_csi.get_sensor_timestamps()
            sensor_timestamps = np.copy(sensor_timestamps_raw)
            if self.calibrate:
                assert(self.pool.get_calibration() is not None)
                sensor_timestamps = self.pool.get_calibration().apply_timestamps(sensor_timestamps)
            self.storage_timestamps[self.head] = sensor_timestamps

            # Store LLTF CSI
            if self.enable_lltf:
                csi_lltf = clustered_csi.deserialize_csi_lltf()
                if self.calibrate:
                    assert(self.pool.get_calibration() is not None)
                    csi_lltf = self.pool.get_calibration().apply_lltf(csi_lltf, sensor_timestamps_raw)

                self.storage_lltf[self.head] = csi_lltf
            else:
                self.storage_ht40[self.head] = np.nan

            # Store HT40 CSI if applicable
            if self.enable_ht40:
                if clustered_csi.is_ht40():
                    csi_ht40 = clustered_csi.deserialize_csi_ht40()
                    if self.calibrate:
                        assert(self.pool.get_calibration() is not None)
                        csi_ht40 = self.pool.get_calibration().apply_ht40(csi_ht40, sensor_timestamps_raw)

                    self.storage_ht40[self.head] = csi_ht40
                else:
                    self.logger.warning(f"Received non-HT40 frame even though HT40 is enabled")
            else:
                self.storage_ht40[self.head] = np.nan

            # Store RSSI
            self.storage_rssi[self.head] = clustered_csi.get_rssi()

            # Store MAC address. mac_str is a hex string without colons, e.g. "00:11:22:33:44:55" -> "001122334455"
            mac_str = clustered_csi.get_source_mac()
            mac = np.asarray([int(mac_str[i:i+2], 16) for i in range(0, len(mac_str), 2)])
            assert(mac.shape == (6,))
            self.storage_macs[self.head] = mac

            # Advance ringbuffer head
            self.latest = self.head
            self.head = (self.head + 1) % self.size
            self.filllevel = min(self.filllevel + 1, self.size)

            for cb in self.callbacks:
                cb()

        self.pool.add_csi_callback(new_csi_callback, cb_predicate = cb_predicate)
        self.callbacks = []
        self.filllevel = 0

        self.mac_filter = None

    def add_update_callback(self, cb):
        """ Add a callback that is called when new CSI data is added to the backlog """
        self.callbacks.append(cb)

    def get_lltf(self):
        """
        Retrieve LLTF CSI data from the ringbuffer

        :return: LLTF CSI data, oldest first
        """
        return np.roll(self.storage_lltf, -self.head, axis = 0)[-self.filllevel:]

    def get_ht40(self):
        """
        Retrieve HT40 CSI data from the ringbuffer

        :return: HT40 CSI data, oldest first
        """
        assert(self.enable_ht40)
        return np.roll(self.storage_ht40, -self.head, axis = 0)[-self.filllevel:]

    def get_rssi(self):
        """
        Retrieve RSSI data from the ringbuffer

        :return: RSSI data, oldest first
        """
        return np.roll(self.storage_rssi, -self.head, axis = 0)[-self.filllevel:]

    def get_timestamps(self):
        """
        Retrieve packet timestamps for all antennas from the ringbuffer

        :return: Timestamps, oldest first, shape (n_packets, n_boards, constants.ROWS_PER_BOARD, constants.ANTENNAS_PER_ROW)
        """
        return np.roll(self.storage_timestamps, -self.head, axis = 0)[-self.filllevel:]

    def get_latest_timestamp(self):
        """
        Retrieve the mean (over all antennas) timestamp of the most recent packet in the ringbuffer

        :return: Timestamp of the most recent packet, scalar
        """
        if self.latest is None:
            return None

        return np.mean(self.storage_timestamps[self.latest])

    def get_macs(self):
        """
        Retrieve MAC addresses from the ringbuffer

        :return: MAC addresses, oldest first
        """
        return np.roll(self.storage_macs, -self.head, axis = 0)[-self.filllevel:]

    def nonempty(self):
        """
        Check if the backlog is nonempty

        :return: True if the backlog is nonempty
        """
        return self.latest is not None

    def start(self):
        """
        Start the CSI backlog thread, must be called before using the backlog
        """
        self.thread = threading.Thread(target=self.__run)
        self.thread.start()
        self.logger.info(f"Started CSI backlog thread")

    def stop(self):
        """
        Stop the CSI backlog thread
        """
        self.running = False
        self.thread.join()

    def set_mac_filter(self, filter_regex):
        """
        Set a MAC address filter for the backlog

        :param filter_regex: MAC address filter regex
        """
        self.mac_filter = re.compile(filter_regex)

    def __run(self):
        """
        CSI backlog thread main loop, do not call directly.

        This function runs in a separate thread and continuously processes CSI data from the pool.
        """
        while self.running:
            self.pool.run()
