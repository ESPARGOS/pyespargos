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
    :param size: Size of the ringbuffer (default: 100)
    """
    def __init__(self, pool, enable_ht40 = True, calibrate = True, size = 100):
        self.logger = logging.getLogger("pyespargos.backlog")

        self.pool = pool
        self.size = size
        self.enable_ht40 = enable_ht40
        self.calibrate = calibrate

        if self.enable_ht40:
            self.storage_csi = np.zeros((size,) + self.pool.get_shape() + ((csi.csi_buf_t.htltf_lower.size + csi.HT40_GAP_SUBCARRIERS * 2 + csi.csi_buf_t.htltf_higher.size) // 2,), dtype = np.complex64)
        else:
            self.storage_csi = np.zeros((size,) + self.pool.get_shape() + ((csi.csi_buf_t.lltf.size) // 2,), dtype = np.complex64)
        self.storage_timestamps = np.zeros((size,) + self.pool.get_shape(), dtype = np.float128)
        self.storage_rssi = np.zeros((size,) + self.pool.get_shape(), dtype = np.float32)
        self.head = 0
        self.latest = None

        self.running = True

        def new_csi_callback(clustered_csi):
            # Check MAC address if filter is installed
            if self.mac_filter is not None:
                if not self.mac_filter.match(clustered_csi.get_source_mac()):
                    return

            # Store timestamp
            sensor_timestamps = clustered_csi.get_sensor_timestamps()
            if self.calibrate:
                assert(self.pool.get_calibration() is not None)
                sensor_timestamps = self.pool.get_calibration().apply_timestamps(sensor_timestamps)
            self.storage_timestamps[self.head] = sensor_timestamps

            # Store HT40 CSI if applicable
            sensor_timestamps_raw = clustered_csi.get_sensor_timestamps()
            if self.enable_ht40 and clustered_csi.is_ht40():
                csi_ht40 = clustered_csi.deserialize_csi_ht40()
                if self.calibrate:
                    assert(self.pool.get_calibration() is not None)
                    csi_ht40 = self.pool.get_calibration().apply_ht40(csi_ht40, sensor_timestamps_raw)

                self.storage_csi[self.head] = csi_ht40
            elif (not self.enable_ht40) and (not clustered_csi.is_ht40()):
                self.storage_ht40[self.head] = csi_ht40
                csi_l20 = clustered_csi.deserialize_csi_lltf()
                if self.calibrate:
                    assert(self.pool.get_calibration() is not None)
                    csi_l20 = self.pool.get_calibration().apply_l20(csi_l20, sensor_timestamps_raw)

                self.storage_csi[self.head] = csi_l20

            # Store RSSI
            self.storage_rssi[self.head] = clustered_csi.get_rssi()

            # Advance ringbuffer head
            self.latest = self.head
            self.head = (self.head + 1) % self.size
            self.filllevel = min(self.filllevel + 1, self.size)

            for cb in self.callbacks:
                cb()

        self.pool.add_csi_callback(new_csi_callback)
        self.callbacks = []
        self.filllevel = 0

        self.mac_filter = None

    def add_update_callback(self, cb):
        """ Add a callback that is called when new CSI data is added to the backlog """
        self.callbacks.append(cb)

    def get_ht40(self):
        """
        Retrieve HT40 CSI data from the ringbuffer

        :return: HT40 CSI data, oldest first
        """
        assert(self.enable_ht40)
        return np.roll(self.storage_csi, -self.head, axis = 0)[-self.filllevel:]
    

    def get_csi(self):
        """
        Retrieve CSI data from the ringbuffer

        :return: CSI data, oldest first
        """
        return np.roll(self.storage_csi, -self.head, axis = 0)[-self.filllevel:]

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
