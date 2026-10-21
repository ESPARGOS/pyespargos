#!/usr/bin/env python

"""Queue sensor messages, assemble related measurements and dispatch callbacks.

Pool.run() drains the queue in bounded batches. Subclasses define cluster keys
and caches, such as separate CSI caches for ordinary and reference packets.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable
from weakref import WeakKeyDictionary
import logging
import queue
import threading
import time

from . import board
from . import constants
from . import sensor
from .sensor_cluster import ClusterCollisionError, SensorCluster

__all__ = ["ClusterAssembler"]

_DEFAULT_RUN_TIMEOUT = 0.005
_RUN_BATCH_BOUNDARY = object()
_CLUSTER_COLLISION_WARNING_INTERVAL = 5.0


class _ClusterCallback(object):
    """One registered cluster-completion callback and its fired-state tracking."""

    def __init__(
        self,
        callback: Callable[[SensorCluster], None],
        callback_predicate: Callable[[SensorCluster], bool] = None,
    ):
        self._callback_predicate = callback_predicate
        self._callback = callback

        self._fired = WeakKeyDictionary()

    def _try_call(self, cluster_obj: SensorCluster):
        if self._fired.get(cluster_obj, False):
            return True

        callback_required = False
        if self._callback_predicate is not None:
            callback_required = self._callback_predicate(cluster_obj)
        else:
            callback_required = cluster_obj.is_complete

        if callback_required:
            self._callback(cluster_obj)

            self._fired[cluster_obj] = True
            return True

        return False


class ClusterAssembler(ABC):
    """Queue sensor messages, assemble clusters, and dispatch callbacks.

    The owning Pool manages connections and processing. Closing an assembler
    only detaches its subscriptions.
    """

    def __init__(self, owner):
        self._owner = owner
        boards = owner.boards
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

    def _close(self):
        """Detach this receiver from its boards without stopping the boards."""

        subscriptions = self._sensor_message_subscriptions
        self._sensor_message_subscriptions = []
        for capability, subscription in subscriptions:
            capability.unsubscribe(subscription)

    @property
    def processing(self):
        """Whether the owning pool is processing incoming data."""
        return self._owner.processing

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

    @property
    def shape(self) -> tuple[int, int, int]:
        """Return the logical ``(board, row, column)`` sensor-array shape."""

        return (
            len(self.boards),
            constants.ROWS_PER_BOARD,
            constants.ANTENNAS_PER_ROW,
        )

    def _run(self, timeout: float | None = _DEFAULT_RUN_TIMEOUT) -> int:
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
            raise RuntimeError("Receiver processing must not be called concurrently or reentrantly")

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
        if cluster_key is None:
            return None

        collision = None
        with self._cluster_lock:
            cache = self._cluster_caches.setdefault(cache_name, {})
            cluster_key = self._resolve_cluster_key(
                cache_name,
                cluster_key,
                cache,
                board_index,
                sensor_message,
            )
            if cluster_key is None:
                return None
            sensor_cluster = cache.get(cluster_key)
            if sensor_cluster is None:
                sensor_cluster = self._create_cluster(
                    cache_name,
                    cluster_key,
                    board_index,
                    sensor_message,
                )
                cache[cluster_key] = sensor_cluster
                self._on_cluster_cached(cache_name, cluster_key, sensor_cluster)

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
                self._on_cluster_removed(cache_name, cluster_key, sensor_cluster)

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
                    expired.append((cache_name, cluster_key, cache[cluster_key]))

        # Notify outside the cluster lock: the hook may offer the cluster to
        # consumer callbacks, which are free to call back into the pool. Most
        # clusters are then evicted. A subclass may retain the same object for
        # one fresh timeout interval when late transport repair must add to it.
        for cache_name, cluster_key, sensor_cluster in expired:
            retain = self._on_cluster_expired(cache_name, cluster_key, sensor_cluster)
            if retain:
                with self._cluster_lock:
                    cache = self._cluster_caches.get(cache_name)
                    if cache is not None and cache.get(cluster_key) is sensor_cluster:
                        sensor_cluster._restart_age()
            else:
                self._remove_cluster_if_current(cache_name, cluster_key, sensor_cluster)

    def _clear_cluster_cache(self, cache_name: str) -> None:
        """Clear a named cache if it exists."""

        with self._cluster_lock:
            cache = self._cluster_caches.get(cache_name)
            if cache is not None:
                cache.clear()
                self._on_cluster_cache_cleared(cache_name)

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
    ) -> Hashable | None:
        """Return the logical-observation key, or ``None`` to drop a message."""

    def _resolve_cluster_key(
        self,
        cache_name: str,
        proposed_key: Hashable,
        cache: dict[Hashable, SensorCluster],
        board_index: int,
        sensor_message: sensor.SensorMessage,
    ) -> Hashable | None:
        """Resolve a proposed key against the cache while holding its lock.

        Exact-key clustering uses the proposed key unchanged.  Subclasses may
        override this to select among several open instances of a repeatable
        signature, as CSI control-frame timestamp association does.  Returning
        ``None`` conservatively drops an ambiguous observation.
        """

        return proposed_key

    def _on_cluster_cached(
        self,
        cache_name: str,
        cluster_key: Hashable,
        sensor_cluster: SensorCluster,
    ) -> None:
        """Called after a cluster is inserted, with the cache lock held."""

    def _on_cluster_removed(
        self,
        cache_name: str,
        cluster_key: Hashable,
        sensor_cluster: SensorCluster,
    ) -> None:
        """Called after a cluster is removed, with the cache lock held."""

    def _on_cluster_cache_cleared(self, cache_name: str) -> None:
        """Called after a cache is cleared, with the cache lock held."""

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
    ) -> bool:
        """Handle a cluster that reached its cache timeout.

        The default silently discards stale incomplete measurements. A
        subclass whose consumers also want partial measurements can override
        this and offer the cluster to callbacks via :meth:`_try_callbacks`.
        Return true to retain the same object for one fresh timeout interval,
        allowing a bounded late repair to add missing positions; false/None
        evicts it. Runs on the :meth:`run` caller's thread, like ordinary
        completion callbacks.
        """

        return False
