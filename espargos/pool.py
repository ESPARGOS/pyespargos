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
import json
import logging
import queue
import threading
import time

from . import board
from . import constants
from . import sensor
from .sensor_cluster import ClusterCollisionError, SensorCluster

_DEFAULT_RUN_TIMEOUT = 0.005
_RUN_BATCH_BOUNDARY = object()
_CLUSTER_COLLISION_WARNING_INTERVAL = 5.0


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
        self.logger = logging.getLogger(f"pyespargos.{module_name}")
        self.boards = list(boards)
        self.board_revisions = tuple(board_obj.revision for board_obj in self.boards)

        self._input_queue = queue.Queue()
        self._run_lock = threading.Lock()
        self._cluster_lock = threading.Lock()
        self._cluster_caches: dict[str, dict[Hashable, SensorCluster]] = {}
        self._sensor_message_subscriptions: list[tuple[board.BoardCapability, board.SensorMessageSubscription]] = []
        self._cluster_collisions_since_warning = 0
        self._last_cluster_collision_warning: float | None = None

        self.logger.info(f"Created new {type(self).__name__} with {len(self.boards)} board(s)")

    def _subscribe_sensor_messages(
        self,
        board_num: int,
        capability: board.BoardCapability,
        subscribe: Callable[[Callable[[sensor.SensorMessage], None]], board.SensorMessageSubscription],
    ) -> None:
        """Subscribe one decoded message type and enqueue it for this pool."""

        def enqueue(sensor_message: sensor.SensorMessage):
            self._input_queue.put_nowait((board_num, sensor_message))

        subscription = subscribe(enqueue)
        self._sensor_message_subscriptions.append((capability, subscription))

    def start(self):
        """Start sensor-message reception on every board."""

        for board_obj in self.boards:
            board_obj.start()

    def stop(self):
        """Stop sensor-message reception on every board."""

        for board_obj in self.boards:
            board_obj.stop()

    def close(self):
        """Detach this pool from its boards without stopping the boards."""

        subscriptions = self._sensor_message_subscriptions
        self._sensor_message_subscriptions = []
        for capability, subscription in subscriptions:
            capability.unsubscribe(subscription)

    def reboot(self):
        """Reboot every board in the pool."""

        for board_obj in self.boards:
            board_obj.reboot()

    def get_shape(self) -> tuple[int, int, int]:
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
            for board_num, sensor_message in messages:
                updated = self._add_sensor_message(board_num, sensor_message)
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
        board_num: int,
        sensor_message: sensor.SensorMessage,
    ) -> tuple[str, Hashable, SensorCluster] | None:
        cache_name = self._get_cluster_cache_name(board_num, sensor_message)
        cluster_key = self._get_cluster_key(board_num, sensor_message)

        collision = None
        with self._cluster_lock:
            cache = self._cluster_caches.setdefault(cache_name, {})
            sensor_cluster = cache.get(cluster_key)
            if sensor_cluster is None:
                sensor_cluster = self._create_cluster(
                    cache_name,
                    cluster_key,
                    board_num,
                    sensor_message,
                )
                cache[cluster_key] = sensor_cluster

            try:
                changed = sensor_cluster.add_message(board_num, sensor_message)
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
        with self._cluster_lock:
            for cache_name, timeout in cache_timeouts.items():
                cache = self._cluster_caches.get(cache_name)
                if not cache:
                    continue
                expired_keys = [cluster_key for cluster_key, sensor_cluster in cache.items() if sensor_cluster.get_age() > timeout]
                for cluster_key in expired_keys:
                    cache.pop(cluster_key)

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

        self.logger.warning(
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
        self.logger.warning(f"{what}: boards disagree (board 0 != board(s) {mismatched}); resetting all boards to {target}.")
        try:
            apply(apply_value)
        except Exception as error:
            self.logger.warning(f"{what}: could not reset boards to a consistent value ({error}); using board 0's value.")
            return values[0]
        return chosen

    @abstractmethod
    def _get_cluster_cache_name(
        self,
        board_num: int,
        sensor_message: sensor.SensorMessage,
    ) -> str:
        """Return the named cache that should receive a message."""

    @abstractmethod
    def _get_cluster_key(
        self,
        board_num: int,
        sensor_message: sensor.SensorMessage,
    ) -> Hashable:
        """Return the logical-observation key for a message."""

    @abstractmethod
    def _create_cluster(
        self,
        cache_name: str,
        cluster_key: Hashable,
        board_num: int,
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
