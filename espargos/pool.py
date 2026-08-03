#!/usr/bin/env python

"""Receive sensor messages from one or more boards and group related data.

Each ESPARGOS sensor reports its measurements independently. For example, when
one Wi-Fi packet reaches all eight sensors of a board, the host receives eight
separate sensor messages containing CSI for that packet. A pool collects those
messages and a pool subclass uses packet metadata to determine that they belong
to the same cluster.

Messages are first placed in a thread-safe input queue. :meth:`Pool.run`
processes them in bounded batches and adds each message to its cluster. Clusters
that are still being assembled are kept in named caches; a CSI pool uses
separate caches for ordinary over-the-air packets and calibration packets. The
base class does not prescribe a particular sensor-message format; subclasses
provide the cache, key, and cluster behavior for the messages they support.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable
from weakref import WeakKeyDictionary
import json
import logging
import queue
import threading
import time

from . import board
from . import constants
from . import sensor
from .sensor_cluster import ClusterCollisionError, SensorCluster

__all__ = ["Pool"]

_DEFAULT_RUN_TIMEOUT = 0.005
_PROCESSING_RUN_TIMEOUT = 0.5
_RUN_BATCH_BOUNDARY = object()
_CLUSTER_COLLISION_WARNING_INTERVAL = 5.0


class _ClusterCallback(object):
    """One registered cluster-completion callback and its fired-state tracking."""

    def __init__(
        self,
        callback: Callable[[SensorCluster], None],
        callback_predicate: Callable[[SensorCluster], bool] = None,
    ):
        # By default, emit a cluster once every sensor has contributed to it
        self._callback_predicate = callback_predicate
        self._callback = callback

        # Track fired state per cluster object
        self._fired = WeakKeyDictionary()

    def _try_call(self, cluster_obj: SensorCluster):
        # Check if callback has already been fired for this cluster object
        if self._fired.get(cluster_obj, False):
            return True

        # Check if callback needs to be called: Use predicate function if defined, otherwise call if cluster is complete
        callback_required = False
        if self._callback_predicate is not None:
            callback_required = self._callback_predicate(cluster_obj)
        else:
            callback_required = cluster_obj.is_complete

        if callback_required:
            self._callback(cluster_obj)

            # Mark as fired for this cluster object
            self._fired[cluster_obj] = True
            return True

        return False


class Pool(ABC):
    """Manage boards, message reception, and the lifecycle of sensor clusters.

    ``Pool`` provides the mechanics shared by different measurement types. It
    starts and stops the boards, queues messages received by their stream
    threads, and retains incomplete clusters. A subclass defines how messages
    are identified and combined. For example, :class:`.CSIPool` groups CSI
    reports from different sensors by the Wi-Fi frame's MAC addresses and
    sequence control field.
    """

    def __init__(self, boards: list[board.Board]):
        module_name = type(self).__module__.rsplit(".", maxsplit=1)[-1]
        self._logger = logging.getLogger(f"pyespargos.{module_name}")
        self.boards = list(boards)
        self.board_revisions = tuple(board_obj.revision for board_obj in self.boards)

        self._input_queue = queue.Queue()
        self._run_lock = threading.Lock()
        self._cluster_lock = threading.Lock()
        self._cluster_caches: dict[str, dict[Hashable, SensorCluster]] = {}
        self._sensor_message_subscriptions: list[tuple[board.BoardCapability, board.SensorMessageSubscription]] = []
        self._cluster_collisions_since_warning = 0
        self._last_cluster_collision_warning: float | None = None
        self._cluster_callbacks: list[_ClusterCallback] = []
        self._cluster_callbacks_lock = threading.Lock()
        self._processing_thread: threading.Thread | None = None
        self._processing_lock = threading.Lock()
        self._processing = False

        self._logger.info(f"Created new {type(self).__name__} with {len(self.boards)} board(s)")

    def _subscribe_sensor_messages(
        self,
        board_index: int,
        capability: board.BoardCapability,
        subscribe: Callable[[Callable[[sensor.SensorMessage], None]], board.SensorMessageSubscription],
    ) -> None:
        """Subscribe one decoded message type and enqueue it for this pool."""

        def enqueue(sensor_message: sensor.SensorMessage):
            self._input_queue.put_nowait((board_index, sensor_message))

        subscription = subscribe(enqueue)
        self._sensor_message_subscriptions.append((capability, subscription))

    def start(self):
        """Start sensor-message reception on every board.

        Each board's sensor stream is a single shared resource and
        :meth:`.Board.start` is idempotent, so several pools over the same
        boards may all call :meth:`start` safely.
        """

        for board_obj in self.boards:
            board_obj.start()

    def stop(self):
        """Stop sensor-message reception on every board.

        This tears down each board's shared sensor stream for every consumer.
        A pool that shares its boards with other consumers should use
        :meth:`close` instead and leave stopping the boards to whoever owns
        their lifecycle.
        """

        for board_obj in self.boards:
            board_obj.stop()

    def close(self):
        """Detach this pool from its boards without stopping the boards."""

        self.stop_processing()
        subscriptions = self._sensor_message_subscriptions
        self._sensor_message_subscriptions = []
        for capability, subscription in subscriptions:
            capability.unsubscribe(subscription)

    def start_processing(self):
        """Start a worker thread that continuously processes this pool.

        The worker calls :meth:`run` in a loop until :meth:`stop_processing`
        is called. Since :meth:`run` must not be called concurrently, the pool
        has at most one processing worker, and starting it again while it is
        running is a no-op. Alternatively, consumers may drive :meth:`run`
        from their own thread and leave this worker unused.
        """

        with self._processing_lock:
            if self._processing_thread is not None and self._processing_thread.is_alive():
                return
            self._processing = True
            self._processing_thread = threading.Thread(
                target=self._processing_loop,
                name=f"{type(self).__name__.lower()}-processing",
                daemon=True,
            )
            self._processing_thread.start()
            self._logger.info(f"Started {type(self).__name__} processing thread")

    def stop_processing(self):
        """Stop the pool-processing worker, if running."""

        with self._processing_lock:
            thread = self._processing_thread
            self._processing = False
            self._processing_thread = None
        if thread is not None:
            thread.join()

    def _processing_loop(self):
        while self._processing:
            self.run(timeout=_PROCESSING_RUN_TIMEOUT)

    def add_cluster_callback(
        self,
        callback: Callable[[SensorCluster], None],
        callback_predicate: Callable[[SensorCluster], bool] = None,
    ) -> _ClusterCallback:
        """Register a callback invoked when a sensor cluster is completed.

        Completion is decided by ``callback_predicate``; without a predicate, a
        cluster is emitted once every sensor has contributed to it. Each
        callback fires at most once per cluster. The registry only stores the
        callbacks: the pool subclass decides from :meth:`_on_cluster_updated`
        which updated clusters are offered to them via :meth:`_try_callbacks`.

        :param callback: The function to call, gets the completed :class:`.SensorCluster`
        :param callback_predicate: A function with signature :code:`(sensor_cluster)` that defines the conditions under which
            a cluster is regarded as completed and thus provided to the callback.
        :return: A callback handle that can be passed to :meth:`remove_cluster_callback`
        """

        callback_handle = _ClusterCallback(callback, callback_predicate)
        with self._cluster_callbacks_lock:
            self._cluster_callbacks.append(callback_handle)
        return callback_handle

    def remove_cluster_callback(self, callback: _ClusterCallback) -> bool:
        """Remove a callback previously returned by :meth:`add_cluster_callback`.

        :param callback: Callback handle returned by :meth:`add_cluster_callback`
        :return: True if the callback was registered and removed, False otherwise
        """

        with self._cluster_callbacks_lock:
            try:
                self._cluster_callbacks.remove(callback)
                return True
            except ValueError:
                return False

    def replace_cluster_callback(
        self,
        callback: _ClusterCallback,
        callback_function: Callable[[SensorCluster], None],
        callback_predicate: Callable[[SensorCluster], bool] = None,
    ) -> _ClusterCallback:
        """Atomically replace a registered cluster callback.

        :param callback: Existing handle returned by :meth:`add_cluster_callback`
        :param callback_function: Replacement callback function
        :param callback_predicate: Replacement completion predicate
        :return: New callback handle
        :raises ValueError: If ``callback`` is no longer registered
        """

        replacement = _ClusterCallback(callback_function, callback_predicate)
        with self._cluster_callbacks_lock:
            try:
                index = self._cluster_callbacks.index(callback)
            except ValueError:
                raise ValueError("Cluster callback is not registered") from None
            self._cluster_callbacks[index] = replacement
        return replacement

    def _try_callbacks(self, cluster_obj: SensorCluster) -> bool:
        """Offer a cluster to every registered callback; return whether all have fired."""

        all_callbacks_fired = True
        with self._cluster_callbacks_lock:
            callbacks = tuple(self._cluster_callbacks)
        for callback in callbacks:
            callback_fired = callback._try_call(cluster_obj)
            all_callbacks_fired = callback_fired and all_callbacks_fired
        return all_callbacks_fired

    def reboot(self):
        """Reboot every board in the pool."""

        for board_obj in self.boards:
            board_obj.reboot()

    @property
    def shape(self) -> tuple[int, int, int]:
        """Return the logical ``(board, row, column)`` sensor-array shape."""

        return (
            len(self.boards),
            constants.ROWS_PER_BOARD,
            constants.ANTENNAS_PER_ROW,
        )

    def run(self, timeout: float | None = _DEFAULT_RUN_TIMEOUT) -> int:
        """Process one bounded batch of pending sensor messages.

        If no message is immediately available, wait up to ``timeout`` seconds
        for the first one. Once a message is available, all messages ahead of a
        FIFO batch boundary are processed together. Messages arriving after the
        boundary remain queued for the next call. In the CSI case, one batch
        commonly contains reports from several sensors for the same Wi-Fi
        packet, possibly together with reports for other packets.

        Completion checks are performed once per updated cluster after the
        batch, rather than after every individual sensor report. Calls must not
        overlap; concurrent or reentrant calls raise :class:`RuntimeError`.

        :return: Number of sensor messages processed.
        """

        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Pool.run() must not be called concurrently or reentrantly")

        try:
            try:
                first_message = self._input_queue.get(timeout=timeout)
            except queue.Empty:
                self._expire_cluster_caches()
                return 0

            messages = [first_message]
            self._input_queue.put_nowait(_RUN_BATCH_BOUNDARY)
            while True:
                message = self._input_queue.get_nowait()
                if message is _RUN_BATCH_BOUNDARY:
                    break
                messages.append(message)

            # A cluster commonly receives several sensor messages in one
            # batch. Coalesce those updates by object identity so the
            # subclass's completion checks run only once, without repeatedly
            # hashing potentially structured cluster keys.
            updated_clusters: dict[int, tuple[str, Hashable, SensorCluster]] = {}
            for board_index, sensor_message in messages:
                updated = self._add_sensor_message(board_index, sensor_message)
                if updated is not None:
                    cache_name, cluster_key, sensor_cluster = updated
                    updated_clusters[id(sensor_cluster)] = updated

            for cache_name, cluster_key, sensor_cluster in updated_clusters.values():
                if self._on_cluster_updated(cache_name, cluster_key, sensor_cluster):
                    self._remove_cluster_if_current(cache_name, cluster_key, sensor_cluster)

            self._expire_cluster_caches()
            return len(messages)
        finally:
            self._run_lock.release()

    def _add_sensor_message(
        self,
        board_index: int,
        sensor_message: sensor.SensorMessage,
    ) -> tuple[str, Hashable, SensorCluster] | None:
        cache_name = self._get_cluster_cache_name(board_index, sensor_message)
        cluster_key = self._get_cluster_key(board_index, sensor_message)

        collision = None
        with self._cluster_lock:
            cache = self._cluster_caches.setdefault(cache_name, {})
            sensor_cluster = cache.get(cluster_key)
            if sensor_cluster is None:
                sensor_cluster = self._create_cluster(
                    cache_name,
                    cluster_key,
                    board_index,
                    sensor_message,
                )
                cache[cluster_key] = sensor_cluster

            try:
                changed = sensor_cluster.add_message(board_index, sensor_message)
            except ClusterCollisionError as error:
                collision = error

        if collision is not None:
            self._warn_cluster_collision(cache_name, cluster_key, collision)
            return None
        if not changed:
            return None
        return cache_name, cluster_key, sensor_cluster

    def _remove_cluster_if_current(
        self,
        cache_name: str,
        cluster_key: Hashable,
        sensor_cluster: SensorCluster,
    ) -> None:
        with self._cluster_lock:
            cache = self._cluster_caches.get(cache_name)
            if cache is not None and cache.get(cluster_key) is sensor_cluster:
                cache.pop(cluster_key)

    def _expire_cluster_caches(self) -> None:
        with self._cluster_lock:
            cache_names = tuple(self._cluster_caches)

        cache_timeouts = {cache_name: timeout for cache_name in cache_names if (timeout := self._get_cluster_cache_timeout(cache_name)) is not None}
        if not cache_timeouts:
            return

        # Only inspect expiring caches. In particular, retained calibration
        # clusters can be numerous and must not be copied on every run() call.
        expired: list[tuple[str, Hashable, SensorCluster]] = []
        with self._cluster_lock:
            for cache_name, timeout in cache_timeouts.items():
                cache = self._cluster_caches.get(cache_name)
                if not cache:
                    continue
                expired_keys = [cluster_key for cluster_key, sensor_cluster in cache.items() if sensor_cluster.age > timeout]
                for cluster_key in expired_keys:
                    expired.append((cache_name, cluster_key, cache.pop(cluster_key)))

        # Notify outside the cluster lock: the hook may offer the cluster to
        # consumer callbacks, which are free to call back into the pool.
        for cache_name, cluster_key, sensor_cluster in expired:
            self._on_cluster_expired(cache_name, cluster_key, sensor_cluster)

    def _clear_cluster_cache(self, cache_name: str) -> None:
        """Clear a named cache if it exists."""

        with self._cluster_lock:
            cache = self._cluster_caches.get(cache_name)
            if cache is not None:
                cache.clear()

    def _get_cluster_cache_snapshot(self, cache_name: str) -> list[SensorCluster]:
        """Return a stable list of clusters currently in a named cache."""

        with self._cluster_lock:
            return list(self._cluster_caches.get(cache_name, {}).values())

    def _warn_cluster_collision(
        self,
        cache_name: str,
        cluster_key: Hashable,
        collision: ClusterCollisionError,
    ) -> None:
        self._cluster_collisions_since_warning += 1
        now = time.monotonic()
        if self._last_cluster_collision_warning is not None and now - self._last_cluster_collision_warning < _CLUSTER_COLLISION_WARNING_INTERVAL:
            return

        self._logger.warning(
            "Cluster-key collision in cache %r for %r: %s; dropping the incoming "
            "message and retaining the existing cluster. This can happen when a "
            "transmitter does not advance its sequence number as intended. "
            "(%d collision(s) since the previous warning)",
            cache_name,
            cluster_key,
            collision,
            self._cluster_collisions_since_warning,
        )
        self._cluster_collisions_since_warning = 0
        self._last_cluster_collision_warning = now

    def _assert_same_across_boards(self, values: list, what: str):
        """Ensure all values are equal after canonical JSON encoding."""

        if not values:
            raise ValueError(f"{what}: no boards in pool")

        def canonical(value):
            if isinstance(value, (dict, list)):
                return json.dumps(value, sort_keys=True, separators=(",", ":"))
            return value

        reference = canonical(values[0])
        for index, value in enumerate(values[1:], start=1):
            if canonical(value) != reference:
                raise ValueError(f"{what}: mismatch between boards (board 0 != board {index})")

    def _reconcile_across_boards(
        self,
        values: list,
        what: str,
        apply,
        reset_value=None,
        ignore_keys: set[str] | None = None,
    ):
        """Return a consistent setting, reconciling mismatched boards."""

        if not values:
            raise ValueError(f"{what}: no boards in pool")

        def canonical(value):
            if ignore_keys and isinstance(value, dict):
                value = {key: item for key, item in value.items() if key not in ignore_keys}
            if isinstance(value, (dict, list)):
                return json.dumps(value, sort_keys=True, separators=(",", ":"))
            return value

        reference = canonical(values[0])
        mismatched = [index for index, value in enumerate(values[1:], start=1) if canonical(value) != reference]
        if not mismatched:
            return values[0]

        chosen = values[0] if reset_value is None else reset_value
        apply_value = chosen
        if ignore_keys and isinstance(apply_value, dict):
            apply_value = {key: value for key, value in apply_value.items() if key not in ignore_keys}

        target = "board 0's value" if reset_value is None else "a safe default"
        self._logger.warning(f"{what}: boards disagree (board 0 != board(s) {mismatched}); resetting all boards to {target}.")
        try:
            apply(apply_value)
        except Exception as error:
            self._logger.warning(f"{what}: could not reset boards to a consistent value ({error}); using board 0's value.")
            return values[0]
        return chosen

    @abstractmethod
    def _get_cluster_cache_name(
        self,
        board_index: int,
        sensor_message: sensor.SensorMessage,
    ) -> str:
        """Return the named cache that should receive a message."""

    @abstractmethod
    def _get_cluster_key(
        self,
        board_index: int,
        sensor_message: sensor.SensorMessage,
    ) -> Hashable:
        """Return the logical-observation key for a message."""

    @abstractmethod
    def _create_cluster(
        self,
        cache_name: str,
        cluster_key: Hashable,
        board_index: int,
        first_message: sensor.SensorMessage,
    ) -> SensorCluster:
        """Create a cluster for a previously unseen key."""

    @abstractmethod
    def _on_cluster_updated(
        self,
        cache_name: str,
        cluster_key: Hashable,
        sensor_cluster: SensorCluster,
    ) -> bool:
        """Process an updated cluster and return whether it can be removed."""

    def _get_cluster_cache_timeout(self, cache_name: str) -> float | None:
        """Return a cache timeout in seconds, or ``None`` to retain clusters."""

        return None

    def _on_cluster_expired(
        self,
        cache_name: str,
        cluster_key: Hashable,
        sensor_cluster: SensorCluster,
    ) -> None:
        """A cluster timed out before completing and was evicted from its cache.

        The default silently discards stale incomplete measurements. A
        subclass whose consumers also want partial measurements (every sensor
        that reported by the deadline, missing ones absent) can override this
        and offer the evicted cluster to its callbacks via
        :meth:`_try_callbacks`. Runs on the :meth:`run` caller's thread, like
        ordinary completion callbacks.
        """
