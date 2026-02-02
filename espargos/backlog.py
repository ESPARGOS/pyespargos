import numpy as np
import threading
import logging
import re

from . import csi

class CSIBacklog(object):
    """
    CSI backlog class. Stores CSI data in a ringbuffer for processing when needed.

    :param pool: CSI pool object to collect CSI data from
    :param enable: List of fields to store (default: all)
    :param calibrate: Apply calibration to CSI data (default: True)
    :param cb_predicate: A function that defines the conditions under which clustered CSI is regarded as completed and thus added to the backlog.
        See :meth:`espargos.pool.Pool.add_csi_callback` for more details.
    :param size: Size of the ringbuffer (default: 100)
    """
    DATA_FORMATS = {
        "lltf": {
            "shape": (csi.LEGACY_COEFFICIENTS_PER_CHANNEL,),
            "per_antenna": True,
            "dtype": np.complex64
        },
        "ht20": {
            "shape": (csi.HT_COEFFICIENTS_PER_CHANNEL,),
            "per_antenna": True,
            "dtype": np.complex64
        },
        "ht40": {
            "shape": (csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS + csi.HT_COEFFICIENTS_PER_CHANNEL,),
            "per_antenna": True,
            "dtype": np.complex64
        },
        "rssi": {
            "shape": (),
            "per_antenna": True,
            "dtype": np.float32
        },
        "timestamp": {
            "shape": (),
            "per_antenna": True,
            "dtype": np.float128
        },
        "host_timestamp": {
            "shape": (),
            "per_antenna": False,
            "dtype": np.float128
        },
        "mac": {
            "shape": (6,),
            "per_antenna": False,
            "dtype": np.uint8
        }
    }

    def __init__(self, pool, enable = None, calibrate = True, cb_predicate = None, size = 100):
        self.logger = logging.getLogger("pyespargos.backlog")

        self.pool = pool
        self.size = size
        self.enable = set(self.DATA_FORMATS.keys()) if enable is None else set(enable)
        self.calibrate = calibrate

        self.storage = {}
        self.storage_mutex = threading.Lock()
        for key, meta in self.DATA_FORMATS.items():
            shape = meta["shape"]
            dtype = meta["dtype"]
            if meta["per_antenna"]:
                full_shape = (size,) + self.pool.get_shape() + shape
            else:
                full_shape = (size,) + shape

            if dtype in [np.uint8]:
                self.storage[key] = np.zeros(full_shape, dtype = dtype)
            else:
                self.storage[key] = np.full(full_shape, fill_value=np.nan, dtype = dtype)

        self.head = 0
        self.latest = None
        self.filllevel = 0
        self.mac_filter = None

        self.running = True

        def new_csi_callback(clustered_csi):
            # Check MAC address if filter is installed
            if self.mac_filter is not None:
                if not self.mac_filter.match(clustered_csi.get_source_mac()):
                    return

            with self.storage_mutex:
                # Store timestamp
                sensor_timestamps_raw = clustered_csi.get_sensor_timestamps()
                sensor_timestamps = np.copy(sensor_timestamps_raw)
                if self.calibrate:
                    assert(self.pool.get_calibration() is not None)
                    sensor_timestamps = self.pool.get_calibration().apply_timestamps(sensor_timestamps)
                if "timestamp" in self.enable:
                    self.storage["timestamp"][self.head] = sensor_timestamps
                else:
                    self.storage["timestamp"][self.head] = np.nan

                # Store host timestamp
                if "host_timestamp" in self.enable:
                    self.storage["host_timestamp"][self.head] = clustered_csi.get_host_timestamp()
                else:
                    self.storage["host_timestamp"][self.head] = np.nan

                # Store LLTF CSI if applicable
                if "lltf" in self.enable:
                    if clustered_csi.has_lltf():
                        csi_lltf = clustered_csi.deserialize_csi_lltf()
                        if self.calibrate:
                            assert(self.pool.get_calibration() is not None)
                            csi_lltf = self.pool.get_calibration().apply_lltf(csi_lltf)

                        self.storage["lltf"][self.head] = csi_lltf
                    else:
                        self.storage["lltf"][self.head] = np.nan
                        self.logger.warning(f"Received non-LLTF frame even though LLTF is enabled")
                else:
                    self.storage["lltf"][self.head] = np.nan

                # Store HT40 CSI if applicable
                if "ht40" in self.enable:
                    if clustered_csi.has_ht40ltf():
                        csi_ht40 = clustered_csi.deserialize_csi_ht40ltf()

                        if self.calibrate:
                            assert(self.pool.get_calibration() is not None)
                            csi_ht40 = self.pool.get_calibration().apply_ht40(csi_ht40)

                        self.storage["ht40"][self.head] = csi_ht40
                    else:
                        self.storage["ht40"][self.head] = np.nan
                        self.logger.warning(f"Received non-HT40 frame even though HT40 is enabled")
                else:
                    self.storage["ht40"][self.head] = np.nan

                # Store HT20 CSI if applicable
                if "ht20" in self.enable:
                    if clustered_csi.has_ht20ltf():
                        csi_ht20 = clustered_csi.deserialize_csi_ht20ltf()

                        if self.calibrate:
                            assert(self.pool.get_calibration() is not None)
                            csi_ht20 = self.pool.get_calibration().apply_ht20(csi_ht20)

                        self.storage["ht20"][self.head] = csi_ht20
                    else:
                        self.storage["ht20"][self.head] = np.nan
                        self.logger.warning(f"Received non-HT20 frame even though HT20 is enabled")
                else:
                    self.storage["ht20"][self.head] = np.nan

                # Store RSSI
                if "rssi" in self.enable:
                    self.storage["rssi"][self.head] = clustered_csi.get_rssi()
                else:
                    self.storage["rssi"][self.head] = np.nan

                # Store MAC address. mac_str is a hex string without colons, e.g. "00:11:22:33:44:55" -> "001122334455"
                mac_str = clustered_csi.get_source_mac()
                mac = np.asarray([int(mac_str[i:i+2], 16) for i in range(0, len(mac_str), 2)])
                assert(mac.shape == (6,))
                if "mac" in self.enable:
                    self.storage["mac"][self.head] = mac
                else:
                    self.storage["mac"][self.head] = 0

                # Advance ringbuffer head
                self.latest = self.head
                self.head = (self.head + 1) % self.size
                self.filllevel = min(self.filllevel + 1, self.size)

            for cb in self.callbacks:
                cb()

        self.pool.add_csi_callback(new_csi_callback, cb_predicate = cb_predicate)
        self.callbacks = []

    def add_update_callback(self, cb):
        """ Add a callback that is called when new CSI data is added to the backlog """
        self.callbacks.append(cb)

    def get(self, key):
        """
        Retrieve data from the ringbuffer

        :param key: Key of the data to retrieve (e.g., "lltf", "ht40", "rssi", etc.)
        :return: Data corresponding to the key, oldest first
        """
        assert(key in self.enable)

        # Check if storage mutex is held
        if not self.storage_mutex.locked():
            self.logger.warning("get() called without holding storage mutex. This may lead to inconsistent data being read. You must use read_start() and read_finish() to protect read operations.")
        retval = np.copy(np.roll(self.storage[key], -self.head, axis = 0)[-self.filllevel:])

        return retval

    def get_latest(self, key):
        """
        Retrieve the latest value for a key in the ringbuffer.

        :param key: Key of the data to retrieve
        :return: Latest value, or None if no data is available
        """
        if self.latest is None:
            return None

        assert(key in self.enable)
        latest_value = self.storage[key][self.latest]

        return np.copy(latest_value)

    def nonempty(self):
        """
        Check if the backlog is nonempty

        :return: True if the backlog is nonempty
        """
        return self.latest is not None

    def read_start(self):
        """
        Start a read operation from the backlog, must be called before reading data.
        """
        self.storage_mutex.acquire()

    def read_finish(self):
        """
        Finish a read operation from the backlog, must be called after reading data.
        """
        self.storage_mutex.release()

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
