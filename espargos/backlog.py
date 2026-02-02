import numpy as np
import threading
import logging
import re

from . import csi

class CSIBacklog(object):
    """
    CSI backlog class. Stores CSI data in a ringbuffer for processing when needed.

    :param pool: CSI pool object to collect CSI data from
    :param fields: List of fields to store (default: all), e.g., ["lltf", "ht40", "rssi", "timestamp", "host_timestamp", "mac"]
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

    def __init__(self, pool, fields = None, calibrate = True, cb_predicate = None, size = 100):
        self.logger = logging.getLogger("pyespargos.backlog")

        self.pool = pool
        self.calibrate = calibrate

        self.storage_mutex = threading.Lock()
        self.storage = None
        self.head = 0
        self.latest = None
        self.filllevel = 0
        self.mac_filter = None

        self._initialize_storage(size = size, fields = set(self.DATA_FORMATS.keys()) if fields is None else set(fields))

        self.running = True

        self.pool.add_csi_callback(self._on_new_csi, cb_predicate = cb_predicate)
        self.callbacks = []

    def _initialize_storage(self, size=None, fields=None):
        """
        Initialize or reinitialize storage arrays.
        If storage already exists, old data will be preserved where applicable.

        :param size: New size of the ringbuffer (default: keep current size)
        :param fields: New set of fields to store (default: keep current fields)
        """
        with self.storage_mutex:
            # Back up old data if storage exists
            old_storage = None
            if hasattr(self, 'storage') and self.storage:
                old_storage = dict()
                for key in self.fields:
                    old_storage[key] = np.copy(self._read(key))

            # Update size and fields
            if size is not None:
                self.size = size
            if fields is not None:
                self.fields = set(fields)

            # Create new storage
            self.storage = dict()
            for key, meta in self.DATA_FORMATS.items():
                if key not in self.fields:
                    continue

                shape = meta["shape"]
                dtype = meta["dtype"]
                if meta["per_antenna"]:
                    full_shape = (self.size,) + self.pool.get_shape() + shape
                else:
                    full_shape = (self.size,) + shape

                if dtype in [np.uint8]:
                    self.storage[key] = np.zeros(full_shape, dtype=dtype)
                else:
                    self.storage[key] = np.full(full_shape, fill_value=np.nan, dtype=dtype)

            # Reset ringbuffer state
            self.head = 0
            self.latest = None
            self.filllevel = 0

            # Re-insert old data if available
            if old_storage is not None and len(old_storage) > 0:
                num_entries = old_storage[next(iter(old_storage))].shape[0]
                for i in range(num_entries):
                    for key in old_storage.keys():
                        if key in self.fields:
                            self.storage[key][self.head] = old_storage[key][i]

                    self.latest = self.head
                    self.head = (self.head + 1) % self.size
                    self.filllevel = min(self.filllevel + 1, self.size)

    def _on_new_csi(self, clustered_csi):
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

            if "timestamp" in self.fields:
                self.storage["timestamp"][self.head] = sensor_timestamps

            # Store host timestamp
            if "host_timestamp" in self.fields:
                self.storage["host_timestamp"][self.head] = clustered_csi.get_host_timestamp()

            # Store LLTF CSI if applicable
            if "lltf" in self.fields:
                if clustered_csi.has_lltf():
                    csi_lltf = clustered_csi.deserialize_csi_lltf()
                    if self.calibrate:
                        assert(self.pool.get_calibration() is not None)
                        csi_lltf = self.pool.get_calibration().apply_lltf(csi_lltf)

                    self.storage["lltf"][self.head] = csi_lltf
                else:
                    self.storage["lltf"][self.head] = np.nan
                    self.logger.warning(f"Received non-LLTF frame even though LLTF is enabled")

            # Store HT40 CSI if applicable
            if "ht40" in self.fields:
                if clustered_csi.has_ht40ltf():
                    csi_ht40 = clustered_csi.deserialize_csi_ht40ltf()

                    if self.calibrate:
                        assert(self.pool.get_calibration() is not None)
                        csi_ht40 = self.pool.get_calibration().apply_ht40(csi_ht40)

                    self.storage["ht40"][self.head] = csi_ht40
                else:
                    self.storage["ht40"][self.head] = np.nan
                    self.logger.warning(f"Received non-HT40 frame even though HT40 is enabled")

            # Store HT20 CSI if applicable
            if "ht20" in self.fields:
                if clustered_csi.has_ht20ltf():
                    csi_ht20 = clustered_csi.deserialize_csi_ht20ltf()

                    if self.calibrate:
                        assert(self.pool.get_calibration() is not None)
                        csi_ht20 = self.pool.get_calibration().apply_ht20(csi_ht20)

                    self.storage["ht20"][self.head] = csi_ht20
                else:
                    self.storage["ht20"][self.head] = np.nan
                    self.logger.warning(f"Received non-HT20 frame even though HT20 is enabled")

            # Store RSSI
            if "rssi" in self.fields:
                self.storage["rssi"][self.head] = clustered_csi.get_rssi()

            # Store MAC address. mac_str is a hex string without colons, e.g. "00:11:22:33:44:55" -> "001122334455"
            mac_str = clustered_csi.get_source_mac()
            mac = np.asarray([int(mac_str[i:i+2], 16) for i in range(0, len(mac_str), 2)])
            assert(mac.shape == (6,))
            if "mac" in self.fields:
                self.storage["mac"][self.head] = mac

            # Advance ringbuffer head
            self.latest = self.head
            self.head = (self.head + 1) % self.size
            self.filllevel = min(self.filllevel + 1, self.size)

        for cb in self.callbacks:
            cb()

    def add_update_callback(self, cb):
        """ Add a callback that is called when new CSI data is added to the backlog """
        self.callbacks.append(cb)

    def _read(self, key):
        if self.filllevel == 0:
            return np.empty((0,)+self.storage[key].shape[1:], dtype=self.storage[key].dtype)
        return np.roll(self.storage[key], -self.head, axis = 0)[-self.filllevel:]

    def get(self, key):
        """
        Retrieve data from the ringbuffer

        :param key: Key of the data to retrieve (e.g., "lltf", "ht40", "rssi", etc.)
        :return: Data corresponding to the key, oldest first
        """
        assert(key in self.fields)

        self.storage_mutex.acquire()
        retval = np.copy(self._read(key))
        self.storage_mutex.release()

        return retval

    def get_multiple(self, keys):
        """
        Retrieve multiple data fields from the ringbuffer.
        You must use get_multiple to ensure consistency of data across multiple keys.

        :param keys: List of keys of the data to retrieve (e.g., ["lltf", "ht40", "rssi"], etc.)
        :return: Tuple of data arrays corresponding to the keys (in same order), contents are oldest first
        """
        for key in keys:
            assert(key in self.fields)

        self.storage_mutex.acquire()
        retval = []
        for key in keys:
            retval.append(np.copy(self._read(key)))
        self.storage_mutex.release()

        return tuple(retval)

    def get_latest(self, key):
        """
        Retrieve the latest value for a key in the ringbuffer.

        :param key: Key of the data to retrieve
        :return: Latest value, or None if no data is available
        """
        if self.latest is None:
            return None

        assert(key in self.fields)
        latest_value = self.storage[key][self.latest]

        return np.copy(latest_value)

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

    def get_size(self):
        """
        Get the size of the backlog ringbuffer

        :return: Size of the backlog ringbuffer
        """
        return self.size

    def set_size(self, new_size):
        """
        Resize the backlog ringbuffer.
        If there are existing entries, they will be preserved up to the new size.

        :param new_size: New size of the backlog ringbuffer
        """
        self._initialize_storage(size=new_size)

    def set_fields(self, new_fields):
        """
        Set the fields to be stored in the backlog.
        Existing data will be preserved for fields that are still present.

        :param new_fields: New list of fields to store
        """
        self._initialize_storage(fields=new_fields)

    def __run(self):
        """
        CSI backlog thread main loop, do not call directly.

        This function runs in a separate thread and continuously processes CSI data from the pool.
        """
        while self.running:
            self.pool.run()
