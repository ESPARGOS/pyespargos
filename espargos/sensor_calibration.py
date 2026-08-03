#!/usr/bin/env python

"""Per-sensor phase and time calibration, independent of any measurement format.

After calibration, every sensor of an ESPARGOS array is described by two
scalars: a timing offset (its sampling instant relative to the reference
sensor) and a phase offset (its LO phase relative to the reference sensor,
defined at a stated reference frequency). Both re-roll whenever the receivers
re-lock, which is why they must be measured by a calibration run.

:class:`SensorCalibration` stores these offsets together with the per-sensor
reference-path delays and synthesizes the corresponding correction vector for any
frequency grid on demand. It is a data container plus synthesis: how the
offsets are estimated is up to the measurement-format-specific code, e.g.
:meth:`.CSIPool.calibrate`.

Static per-sensor hardware characteristics (such as baseband filter ripple)
are deliberately out of scope: calibration only compensates the phase and
time offsets that change with every lock.
"""

from enum import StrEnum
import logging

import numpy as np

from . import constants

__all__ = ["ClockReferenceScope", "SensorCalibration", "compute_reference_path_delays"]


class ClockReferenceScope(StrEnum):
    """Describe whether sensor clocks share one reference across the pool."""

    POOL = "pool"
    PER_BOARD = "per_board"


def compute_reference_path_delays(boards: list, board_cable_lengths=None, board_cable_vfs=None) -> np.ndarray:
    """Compute the per-sensor propagation delays of the reference signal path.

    The reference/calibration signal reaches each sensor through per-board
    distribution cables (if provided) and the distribution traces on the PCB.
    Both are pure propagation delays that exist only on the reference path,
    independently of the measurement format; the result is meant to be passed
    to :class:`SensorCalibration` as ``reference_path_delays``.

    :param boards: The :class:`.board.Board` objects of the pool; their
        revisions provide the PCB trace delays.
    :param board_cable_lengths: The lengths of the cables that distribute the
        reference signal to the boards, in meters. Omit if all cables have the
        same length.
    :param board_cable_vfs: The velocity factors of those cables. Must be given
        together with ``board_cable_lengths`` and have the same length.
    :return: Per-sensor delays in seconds, with the ``(boards, rows, columns)``
        sensor-array shape.
    """
    cable_group_delays = np.zeros(len(boards), dtype=np.float64)
    if board_cable_lengths is not None:
        assert board_cable_vfs is not None
        assert len(board_cable_lengths) == len(boards)
        assert len(board_cable_vfs) == len(boards)
        board_cable_lengths = np.asarray(board_cable_lengths, dtype=np.float64)
        board_cable_vfs = np.asarray(board_cable_vfs, dtype=np.float64)
        cable_group_delays[:] = board_cable_lengths / (constants.SPEED_OF_LIGHT * board_cable_vfs)

    reference_path_delays = np.zeros(
        (
            len(boards),
            constants.ROWS_PER_BOARD,
            constants.ANTENNAS_PER_ROW,
        ),
        dtype=np.float64,
    )
    for board_index, board_obj in enumerate(boards):
        reference_path_delays[board_index, :, :] = cable_group_delays[board_index] + board_obj.revision.calib_trace_delays

    return reference_path_delays


class SensorCalibration:
    """Store per-sensor phase/time offsets and synthesize corrections for any frequency grid.

    The stored model of each sensor's reference-path response at frequency
    ``f`` is::

        response(f) = exp(j * (phase_offset - 2 * pi * (f - phase_reference_frequency) * timing_offset))

    :meth:`phase_time_correction` returns the conjugate of this response, i.e.
    the vector that removes the per-sensor offsets when multiplied onto
    measured data.

    :param sensor_shape: The logical ``(boards, rows, columns)`` sensor-array shape
    :param timing_offsets: Per-sensor timing offsets in seconds, relative to the reference
        sensor, as a real-valued array of shape ``sensor_shape``. This is the offset of each
        sensor's sampling instant as observed in the received signal phase, which also serves
        as the sensor's clock offset for time conversions (:meth:`time_to_sensor_time`): both
        are observables of the same per-sensor clock, differing only by nanosecond-scale
        latch-path latencies. For a pool-wide clock reference, offsets are relative to sensor
        0 of board 0; for per-board references, relative to sensor 0 of the respective board.
    :param phase_offsets: Per-sensor phase offsets in radians at ``phase_reference_frequency``,
        relative to the reference sensor, as a real-valued array of shape ``sensor_shape``
    :param phase_reference_frequency: The frequency in Hz at which ``phase_offsets`` is defined
    :param clock_scope: Whether ``timing_offsets`` use one pool-wide reference or one
        independent reference per board.
    :param reference_path_delays: Optional per-sensor propagation delays in seconds that exist
        only on the reference/calibration signal path (for example reference distribution
        cables between boards or distribution traces on the PCB), as an array of shape
        ``sensor_shape``. The measured offsets include these delays, but measurement data
        does not travel the reference path, so :meth:`phase_time_correction` compensates
        them on the absolute frequency grid. Applying them at absolute frequencies keeps
        the compensation independent of the phase reference convention.
    """

    def __init__(
        self,
        sensor_shape: tuple[int, int, int],
        timing_offsets: np.ndarray,
        phase_offsets: np.ndarray,
        phase_reference_frequency: float,
        clock_scope: ClockReferenceScope | str = ClockReferenceScope.POOL,
        reference_path_delays: np.ndarray | None = None,
    ):
        self._logger = logging.getLogger("pyespargos.calibration")

        self.sensor_shape = tuple(sensor_shape)
        if len(self.sensor_shape) != 3:
            raise ValueError("sensor_shape must contain three dimensions (boards, rows, columns)")

        self.phase_reference_frequency = float(phase_reference_frequency)
        self.timing_offsets = np.asarray(timing_offsets, dtype=np.float64)
        # Wrap to (-pi, pi] for readability; only the principal value matters
        self.phase_offsets = np.angle(np.exp(1.0j * np.asarray(phase_offsets, dtype=np.float64)))
        self.clock_scope = ClockReferenceScope(clock_scope)
        self.reference_path_delays = np.zeros(self.sensor_shape) if reference_path_delays is None else np.asarray(reference_path_delays, dtype=np.float64)

        for name, array in (
            ("timing_offsets", self.timing_offsets),
            ("phase_offsets", self.phase_offsets),
            ("reference_path_delays", self.reference_path_delays),
        ):
            if array.shape != self.sensor_shape:
                raise ValueError(f"{name} must have the sensor-array shape {self.sensor_shape}, got {array.shape}")

    def phase_time_correction(self, frequencies: np.ndarray) -> np.ndarray:
        """Synthesize the per-sensor correction vector for a frequency grid.

        :param frequencies: Absolute frequencies in Hz, as a one-dimensional array.
        :return: Complex correction values of shape ``sensor_shape + (len(frequencies),)``.
            Multiplying measured data with this vector removes the per-sensor
            phase and time offsets.
        """
        frequencies = np.asarray(frequencies, dtype=np.float64)
        relative_frequencies = frequencies - self.phase_reference_frequency
        response_phase = self.phase_offsets[..., np.newaxis] - 2.0 * np.pi * relative_frequencies[np.newaxis, np.newaxis, np.newaxis, :] * self.timing_offsets[..., np.newaxis]
        # The measured offsets include the reference-path-only delays, which
        # measurement data does not experience; compensate them at the absolute
        # frequencies so the result is independent of the reference convention.
        correction_phase = response_phase + 2.0 * np.pi * self.reference_path_delays[..., np.newaxis] * frequencies[np.newaxis, np.newaxis, np.newaxis, :]
        return np.exp(-1.0j * correction_phase).astype(np.complex64)

    def time_to_sensor_time(self, time):
        """
        Convert a reference time into the corresponding local time for each sensor.

        With a pool-wide clock reference, ``time`` may be a scalar relative to
        sensor 0 of board 0. With per-board clock references, there is no
        meaningful common scalar time: callers must provide one time per board
        using shape ``(boards,)``, ``(boards, 1, 1)``, or the full sensor shape.

        :param time: Reference time or times in seconds.
        :return: Per-sensor time values as a numpy array with the sensor-array shape
        """
        reference_times = np.asarray(time, dtype=np.float64)
        board_count = self.sensor_shape[0]

        if self.clock_scope == ClockReferenceScope.PER_BOARD and board_count > 1:
            if reference_times.shape == (board_count,):
                reference_times = reference_times[:, np.newaxis, np.newaxis]
            elif reference_times.ndim != 3 or reference_times.shape[0] != board_count:
                raise ValueError("This calibration has independent per-board clock references; " "provide one reference time per board using shape (boards,), " "(boards, 1, 1), or the full sensor-array shape")

        try:
            reference_times = np.broadcast_to(
                reference_times,
                self.timing_offsets.shape,
            )
        except ValueError as error:
            raise ValueError("Reference times must be broadcastable to the sensor-array shape") from error

        return reference_times + self.timing_offsets
