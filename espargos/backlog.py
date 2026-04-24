import numpy as np
import threading
import logging
import re

from . import csi


class BacklogFilter(object):
    """
    Base class for CSI backlog filters.

    Subclasses implement :meth:`matches` to decide whether a clustered CSI frame
    should be admitted to the backlog.
    """

    def matches(self, clustered_csi):
        raise NotImplementedError("BacklogFilter subclasses must implement matches()")


class MacFilter(BacklogFilter):
    """
    Backlog filter that matches source MAC addresses against a regular
    expression.

    :param filter_regex: Regular expression applied to the source MAC string
    """

    def __init__(self, filter_regex):
        self.filter_regex = filter_regex
        self._compiled_regex = re.compile(filter_regex)

    def matches(self, clustered_csi):
        return self._compiled_regex.match(clustered_csi.get_source_mac()) is not None


class Exclude11bFilter(BacklogFilter):
    """
    Backlog filter that drops 802.11b packets, which do not carry CSI.
    """

    def matches(self, clustered_csi):
        return not clustered_csi.is_11b()


class CSIBacklog(object):
    """
    CSI backlog class. Stores CSI data in a ringbuffer for processing when needed.

    :param pool: CSI pool object to collect CSI data from
    :param fields: List of fields to store (default: all), e.g., ["lltf", "ht40", "rssi", "cfo", "timestamp", "host_timestamp", "mac", "radar_tx_timestamp", "radar_tx_index"]
    :param calibrate: Apply calibration to CSI data (default: True)
    :param cb_predicate: A function that defines the conditions under which clustered CSI is regarded as completed and thus added to the backlog.
        See :meth:`espargos.pool.Pool.add_csi_callback` for more details.
    :param size: Size of the ringbuffer (default: 100)
    """

    DATA_FORMATS = {
        "lltf": {
            "shape": (csi.LEGACY_COEFFICIENTS_PER_CHANNEL,),
            "per_antenna": True,
            "dtype": np.complex64,
        },
        "ht20": {
            "shape": (csi.HT_COEFFICIENTS_PER_CHANNEL,),
            "per_antenna": True,
            "dtype": np.complex64,
        },
        "ht40": {
            "shape": (csi.HT_COEFFICIENTS_PER_CHANNEL + csi.HT40_GAP_SUBCARRIERS + csi.HT_COEFFICIENTS_PER_CHANNEL,),
            "per_antenna": True,
            "dtype": np.complex64,
        },
        "he20": {
            "shape": (csi.HE20_COEFFICIENTS_PER_CHANNEL,),
            "per_antenna": True,
            "dtype": np.complex64,
        },
        "rssi": {"shape": (), "per_antenna": True, "dtype": np.float32},
        "cfo": {"shape": (), "per_antenna": True, "dtype": np.float32},
        "rfswitch_state": {"shape": (), "per_antenna": True, "dtype": np.uint8},
        "timestamp": {"shape": (), "per_antenna": True, "dtype": np.float64},
        "host_timestamp": {"shape": (), "per_antenna": False, "dtype": np.float64},
        "mac": {"shape": (6,), "per_antenna": False, "dtype": np.uint8},
        "radar_tx_timestamp": {"shape": (), "per_antenna": False, "dtype": np.float64},
        "radar_tx_index": {"shape": (), "per_antenna": False, "dtype": np.int16},
    }

    def __init__(self, pool, fields=None, calibrate=True, cb_predicate=None, size=100):
        self.logger = logging.getLogger("pyespargos.backlog")

        self.pool = pool
        self.calibrate = calibrate

        self.storage_mutex = threading.Lock()
        self.storage = None
        self.head = 0
        self.latest = None
        self.filllevel = 0
        self.filter_mutex = threading.Lock()
        self.filters = []

        self._initialize_storage(
            size=size,
            fields=set(self.DATA_FORMATS.keys()) if fields is None else set(fields),
        )

        self.running = True

        self.pool.add_csi_callback(self._on_new_csi, cb_predicate=cb_predicate)
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
            if hasattr(self, "storage") and self.storage:
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

                if np.issubdtype(dtype, np.unsignedinteger):
                    self.storage[key] = np.zeros(full_shape, dtype=dtype)
                elif np.issubdtype(dtype, np.signedinteger):
                    self.storage[key] = np.full(full_shape, fill_value=-1, dtype=dtype)
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
        with self.filter_mutex:
            filters = tuple(self.filters)

        for backlog_filter in filters:
            if not backlog_filter.matches(clustered_csi):
                return

        with self.storage_mutex:
            # Store timestamp
            sensor_timestamps = clustered_csi.get_sensor_timestamps()

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
                        assert self.pool.get_calibration() is not None
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
                        assert self.pool.get_calibration() is not None
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
                        assert self.pool.get_calibration() is not None
                        csi_ht20 = self.pool.get_calibration().apply_ht20(csi_ht20)

                    self.storage["ht20"][self.head] = csi_ht20
                else:
                    self.storage["ht20"][self.head] = np.nan
                    self.logger.warning(f"Received non-HT20 frame even though HT20 is enabled")

            # Store HE20 CSI if applicable
            if "he20" in self.fields:
                if clustered_csi.has_he20ltf():
                    csi_he20 = clustered_csi.deserialize_csi_he20ltf()

                    if self.calibrate:
                        assert self.pool.get_calibration() is not None
                        csi_he20 = self.pool.get_calibration().apply_he20(csi_he20)

                    self.storage["he20"][self.head] = csi_he20
                else:
                    self.storage["he20"][self.head] = np.nan
                    self.logger.warning(f"Received non-HE20 frame even though HE20 is enabled")

            # Store RSSI
            if "rssi" in self.fields:
                self.storage["rssi"][self.head] = clustered_csi.get_rssi()

            # Store CFO
            if "cfo" in self.fields:
                self.storage["cfo"][self.head] = clustered_csi.get_cfo()

            # Store RF switch states
            if "rfswitch_state" in self.fields:
                self.storage["rfswitch_state"][self.head] = clustered_csi.get_rfswitch_state()

            # Store MAC address. mac_str is a hex string without colons, e.g. "00:11:22:33:44:55" -> "001122334455"
            mac_str = clustered_csi.get_source_mac()
            mac = np.asarray([int(mac_str[i : i + 2], 16) for i in range(0, len(mac_str), 2)])
            assert mac.shape == (6,)
            if "mac" in self.fields:
                self.storage["mac"][self.head] = mac

            # Store radar TX metadata if present. These are packet-wide fields:
            # the TX timestamp is sensor-local, and tx_index is flattened over the pool layout.
            if "radar_tx_timestamp" in self.fields:
                self.storage["radar_tx_timestamp"][self.head] = np.nan
            if "radar_tx_index" in self.fields:
                self.storage["radar_tx_index"][self.head] = -1

            if clustered_csi.has_radar_tx_report():
                radar_tx_report = clustered_csi.get_radar_tx_info()
                if "radar_tx_timestamp" in self.fields:
                    self.storage["radar_tx_timestamp"][self.head] = radar_tx_report.get_hardware_tx_timestamp_ns() / 1e9
                if "radar_tx_index" in self.fields:
                    self.storage["radar_tx_index"][self.head] = clustered_csi.get_radar_tx_index()

            # Advance ringbuffer head
            self.latest = self.head
            self.head = (self.head + 1) % self.size
            self.filllevel = min(self.filllevel + 1, self.size)

        for cb in self.callbacks:
            cb()

    def add_update_callback(self, cb):
        """Add a callback that is called when new CSI data is added to the backlog"""
        self.callbacks.append(cb)

    def _read(self, key):
        if self.filllevel == 0:
            return np.empty((0,) + self.storage[key].shape[1:], dtype=self.storage[key].dtype)
        return np.roll(self.storage[key], -self.head, axis=0)[-self.filllevel :]

    def get(self, key):
        """
        Retrieve data from the ringbuffer

        :param key: Key of the data to retrieve (e.g., "lltf", "ht40", "rssi", etc.)
        :return: Data corresponding to the key, oldest first
        """
        if not key in self.fields:
            raise ValueError(f"Requested key '{key}' not in backlog fields")

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
            if not (key in self.fields):
                raise ValueError(f"Requested key '{key}' not in backlog fields")

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

        assert key in self.fields
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

    def add_filter(self, backlog_filter):
        """
        Add a filter to the backlog.

        :param backlog_filter: Instance of :class:`BacklogFilter`
        """
        if not isinstance(backlog_filter, BacklogFilter):
            raise TypeError("backlog_filter must be an instance of BacklogFilter")

        with self.filter_mutex:
            if backlog_filter not in self.filters:
                self.filters.append(backlog_filter)

    def remove_filter(self, backlog_filter):
        """
        Remove a previously added filter from the backlog.

        :param backlog_filter: Instance of :class:`BacklogFilter`
        """
        with self.filter_mutex:
            if backlog_filter in self.filters:
                self.filters.remove(backlog_filter)

    def clear_filters(self):
        """
        Remove all filters from the backlog.
        """
        with self.filter_mutex:
            self.filters.clear()

    def get_filters(self):
        """
        Get the list of currently active backlog filters.

        :return: List of :class:`BacklogFilter` instances
        """
        with self.filter_mutex:
            return list(self.filters)

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

    def get_fields(self):
        """
        Get the list of fields currently stored in the backlog.

        :return: List of fields currently stored in the backlog
        """
        return self.fields

    def __run(self):
        """
        CSI backlog thread main loop, do not call directly.

        This function runs in a separate thread and continuously processes CSI data from the pool.
        """
        while self.running:
            self.pool.run()
