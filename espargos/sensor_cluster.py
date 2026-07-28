#!/usr/bin/env python

"""Represent measurements that belong together across an ESPARGOS array.

Sensors send their measurements independently, even when they measured the
same event. For example, a Wi-Fi packet received by eight sensors produces
eight CSI sensor messages. A :class:`.CSICluster` collects those messages so
applications can work with the packet as one array-wide measurement. Other
sensor-message formats can define their own cluster subclasses and association
keys without changing the common array handling.

All such clusters need to map firmware antenna IDs to ``(board, row, column)``
positions, remember which sensors have contributed data, and report their age
so a pool can discard stale incomplete data. :class:`SensorCluster` provides
these common parts; subclasses decide what a message contains and whether it is
consistent with messages already stored in the cluster.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
import time

import numpy as np

from . import constants
from . import revisions
from . import sensor


class ClusterCollisionError(RuntimeError):
    """Raised when a cluster already contains different data for a message."""


class SensorCluster(ABC):
    """Related sensor messages assembled into one array-wide measurement."""

    def __init__(self, board_revisions: Sequence[revisions.BoardRevision]):
        self.board_revisions = tuple(board_revisions)
        self.shape = (
            len(self.board_revisions),
            constants.ROWS_PER_BOARD,
            constants.ANTENNAS_PER_ROW,
        )
        self._completion = np.full(self.shape, False, dtype=np.bool_)
        self._created_at = time.monotonic()

    def get_sensor_position(self, board_num: int, antenna_id: int) -> tuple[int, int, int]:
        """Map a firmware antenna ID to ``(board, row, column)``."""

        row, column = self.board_revisions[board_num].antid_to_row_col(antenna_id)
        return board_num, row, column

    def get_completion(self) -> np.ndarray:
        """Return which ``(board, row, column)`` positions supplied data."""

        return self._completion

    def get_completion_all(self) -> bool:
        """Return whether every sensor supplied its part of the measurement."""

        return bool(np.all(self._completion))

    def get_age(self) -> float:
        """Return the approximate cluster age in seconds."""

        return time.monotonic() - self._created_at

    def _mark_sensor_complete(self, board_num: int, antenna_id: int) -> None:
        """Mark the observation data for one sensor as complete."""

        self._mark_sensor_position_complete(
            self.get_sensor_position(board_num, antenna_id)
        )

    def _mark_sensor_position_complete(
        self,
        sensor_position: tuple[int, int, int],
    ) -> None:
        """Mark an already resolved logical sensor position as complete."""

        self._completion[sensor_position] = True

    @abstractmethod
    def add_message(self, board_num: int, sensor_message: sensor.SensorMessage) -> bool:
        """Add one sensor message.

        Return ``True`` if the cluster changed, ``False`` for an exact
        duplicate, and raise :class:`ClusterCollisionError` when the incoming
        message conflicts with data already stored under the same cluster key.
        A collision must leave the existing cluster unchanged.
        """
