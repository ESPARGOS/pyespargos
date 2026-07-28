#!/usr/bin/env python

"""Store completed array-wide sensor measurements in a ring buffer.

Backlogs for different sensor message types need the same storage mechanics:
a bounded and thread-safe history, selectable fields, sensor-array dimensions,
missing-value handling, and update callbacks. They differ in how messages are
clustered and interpreted, which fields they provide, and whether filtering or
calibration is required. Keeping the shared mechanics in
:class:`SensorBacklog` avoids duplicating the ring-buffer implementation while
keeping it independent of any particular sensor message format.

A format-specific backlog supplies the field descriptions and converts each
completed cluster into a mapping passed to
:meth:`SensorBacklog.append_datapoint`. For WiFi packets, for example,
:class:`.CSIBacklog` subscribes to completed CSI clusters, extracts CSI and
packet metadata, and handles CSI-specific filtering and calibration before
appending a datapoint. Fields omitted from a datapoint receive their declared
fill value, so partially available measurements cannot leave stale data
behind when the ring buffer wraps.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
import logging
import threading

import numpy as np


@dataclass(frozen=True)
class BacklogField:
    """Describe one named value stored for every backlog datapoint.

    ``shape`` is the value shape after any sensor-array dimensions.
    ``per_sensor`` prepends the backlog's ``(board, row, column)`` shape.
    ``fill_value`` represents data that is unavailable for one datapoint.
    """

    shape: tuple[int, ...]
    dtype: Any
    per_sensor: bool
    fill_value: Any


class SensorBacklog:
    """Thread-safe ring buffer for named values from sensor clusters."""

    def __init__(
        self,
        sensor_shape: tuple[int, int, int],
        field_specs: Mapping[str, BacklogField],
        fields=None,
        size: int = 100,
        logger: logging.Logger | None = None,
    ):
        self.logger = logger or logging.getLogger("pyespargos.sensor_backlog")
        self.sensor_shape = tuple(sensor_shape)
        if len(self.sensor_shape) != 3 or any(dimension < 0 for dimension in self.sensor_shape):
            raise ValueError("sensor_shape must contain three non-negative dimensions")

        self.field_specs = dict(field_specs)
        if not self.field_specs:
            raise ValueError("field_specs must not be empty")
        for name, spec in self.field_specs.items():
            if not isinstance(name, str) or not name:
                raise ValueError("backlog field names must be non-empty strings")
            if not isinstance(spec, BacklogField):
                raise TypeError(f"field specification for {name!r} must be a BacklogField")

        self.storage_mutex = threading.Lock()
        self._callbacks_lock = threading.Lock()
        self.callbacks: list[Callable[[], None]] = []
        self.storage: dict[str, np.ndarray] = {}
        self.fields: set[str] = set()
        self.size = 0
        self.head = 0
        self.latest: int | None = None
        self.filllevel = 0

        selected_fields = set(self.field_specs) if fields is None else set(fields)
        self._initialize_storage(size=size, fields=selected_fields)

    def _validate_size(self, size: int) -> int:
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError("backlog size must be a positive integer")
        return size

    def _validate_fields(self, fields) -> set[str]:
        selected_fields = set(fields)
        unknown_fields = selected_fields - self.field_specs.keys()
        if unknown_fields:
            unknown = ", ".join(sorted(unknown_fields))
            raise ValueError(f"Unknown backlog field(s): {unknown}")
        return selected_fields

    def _field_shape(self, spec: BacklogField) -> tuple[int, ...]:
        value_shape = tuple(spec.shape)
        return self.sensor_shape + value_shape if spec.per_sensor else value_shape

    def _allocate_field(self, spec: BacklogField) -> np.ndarray:
        return np.full(
            (self.size,) + self._field_shape(spec),
            fill_value=spec.fill_value,
            dtype=np.dtype(spec.dtype),
        )

    def _read_unlocked(self, key: str) -> np.ndarray:
        storage = self.storage[key]
        if self.filllevel == 0:
            return storage[:0]

        first = (self.head - self.filllevel) % self.size
        if first < self.head:
            return storage[first : self.head]
        return np.concatenate((storage[first:], storage[: self.head]), axis=0)

    def _initialize_storage(self, size=None, fields=None) -> None:
        """Reallocate storage while preserving the newest existing datapoints."""

        with self.storage_mutex:
            old_filllevel = self.filllevel
            old_values = {key: np.array(self._read_unlocked(key), copy=True) for key in self.fields}

            if size is not None:
                self.size = self._validate_size(size)
            if fields is not None:
                self.fields = self._validate_fields(fields)

            keep_count = min(old_filllevel, self.size)
            self.storage = {key: self._allocate_field(self.field_specs[key]) for key in self.fields}

            for key, values in old_values.items():
                if key in self.storage and keep_count:
                    self.storage[key][:keep_count] = values[-keep_count:]

            self.filllevel = keep_count
            self.head = keep_count % self.size
            self.latest = keep_count - 1 if keep_count else None

    def append_datapoint(self, values: Mapping[str, Any]) -> None:
        """Atomically append one datapoint.

        Values may be supplied for any known field. Disabled fields are
        ignored, and enabled fields not supplied are reset to their fill value.
        """

        unknown_fields = set(values) - self.field_specs.keys()
        if unknown_fields:
            unknown = ", ".join(sorted(unknown_fields))
            raise ValueError(f"Unknown backlog field(s): {unknown}")

        with self.storage_mutex:
            destination = self.head
            for key in self.fields:
                self.storage[key][destination] = self.field_specs[key].fill_value
            for key, value in values.items():
                if key in self.fields:
                    self.storage[key][destination] = value

            self.latest = destination
            self.head = (destination + 1) % self.size
            self.filllevel = min(self.filllevel + 1, self.size)

        with self._callbacks_lock:
            callbacks = tuple(self.callbacks)
        for callback in callbacks:
            callback()

    def add_update_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback invoked after a datapoint is appended."""

        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._callbacks_lock:
            self.callbacks.append(callback)
        return callback

    def remove_update_callback(self, callback: Callable[[], None]) -> bool:
        """Remove a callback returned by :meth:`add_update_callback`."""

        with self._callbacks_lock:
            try:
                self.callbacks.remove(callback)
                return True
            except ValueError:
                return False

    def get(self, key: str) -> np.ndarray:
        """Return one field for all stored datapoints, oldest first."""

        if key not in self.fields:
            raise ValueError(f"Requested key {key!r} not in backlog fields")
        with self.storage_mutex:
            return np.array(self._read_unlocked(key), copy=True)

    def get_multiple(self, keys) -> tuple[np.ndarray, ...]:
        """Return several fields from one consistent backlog snapshot."""

        keys = tuple(keys)
        missing = [key for key in keys if key not in self.fields]
        if missing:
            raise ValueError(f"Requested key {missing[0]!r} not in backlog fields")
        with self.storage_mutex:
            return tuple(np.array(self._read_unlocked(key), copy=True) for key in keys)

    def get_latest(self, key: str):
        """Return the latest stored value for a field, or ``None``."""

        if key not in self.fields:
            raise ValueError(f"Requested key {key!r} not in backlog fields")
        with self.storage_mutex:
            if self.latest is None:
                return None
            return np.array(self.storage[key][self.latest], copy=True)

    def clear(self) -> None:
        """Discard every stored datapoint."""

        with self.storage_mutex:
            self.head = 0
            self.latest = None
            self.filllevel = 0

    def count_valid_datapoints(
        self,
        key: str,
        allow_incomplete: bool = False,
    ) -> int:
        """Count datapoints with finite values for the requested field."""

        data = self.get(key)
        if data.size == 0:
            return 0

        finite = np.isfinite(data.reshape(data.shape[0], -1))
        valid = np.any(finite, axis=1) if allow_incomplete else np.all(finite, axis=1)
        return int(np.count_nonzero(valid))

    def nonempty(self) -> bool:
        """Return whether the backlog contains at least one datapoint."""

        with self.storage_mutex:
            return self.latest is not None

    def get_size(self) -> int:
        """Return the ring-buffer capacity."""

        with self.storage_mutex:
            return self.size

    def set_size(self, new_size: int) -> None:
        """Resize the ring buffer, preserving its newest datapoints."""

        self._initialize_storage(size=new_size)

    def get_fields(self) -> set[str]:
        """Return the enabled field names."""

        with self.storage_mutex:
            return set(self.fields)

    def set_fields(self, new_fields) -> None:
        """Enable a new field set while preserving the current timeline."""

        self._initialize_storage(fields=new_fields)
