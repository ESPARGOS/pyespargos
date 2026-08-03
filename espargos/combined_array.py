#!/usr/bin/env python

"""Geometry and cabling of phase-coherent multi-board (combined) arrays.

Parses the combined-array configuration, maps sub-array antennas into the
combined layout, models per-board antenna orientations including their
polarization Jones matrices, and handles reference-signal supply cables.
"""

import enum

import numpy as np

__all__ = ["AntennaOrientation", "build_combined_array_data", "build_jones_matrices", "get_cable_wavelength", "parse_combined_array_config"]

from . import constants


class AntennaOrientation(enum.Enum):
    """
    Orientation of a sub-array antenna in the combined array.
    Describes how the board-local coordinate system maps to the combined array coordinate system.
    The naming convention uses compass directions: the orientation indicates which direction the
    board's row axis (short axis of the 2x4 sub-array) points in the combined array.
    The N orientation corresponds to the "default" orientation of the board (4 antennas for azimuth, 2 for elevation),
    where the row axis points downwards and the column axis points to the right (typically used when mounted on tripod).
    """

    N = ((1, 0), (0, 1))  # board_row points down (+row), board_col points right (+col): 0° rotation
    E = ((0, 1), (-1, 0))  # board_row points right (+col), board_col points up (-row): 90° CW rotation
    S = ((-1, 0), (0, -1))  # board_row points up (-row), board_col points left (-col): 180° rotation
    W = ((0, -1), (1, 0))  # board_row points left (-col), board_col points down (+row): 270° CW rotation

    def __init__(self, stride_row, stride_col):
        self.stride_row = stride_row
        self.stride_col = stride_col

    def rotation_matrix(self):
        """
        Returns the 2x2 rotation matrix that maps from the board-local coordinate system
        to the combined array coordinate system. This is the same as the stride matrix.
        """
        return np.array(
            [
                [self.stride_row[0], self.stride_col[0]],
                [self.stride_row[1], self.stride_col[1]],
            ],
            dtype=float,
        )


def build_jones_matrices(antenna_orientations: np.ndarray, base_jones_matrix: np.ndarray = None):
    """
    Build per-antenna effective Jones matrices for a combined array, accounting for the physical
    rotation of each sub-array.

    The effective Jones matrix for each antenna maps from the R/L feed basis to the global H/V
    linear polarization basis, taking into account the antenna's physical orientation.

    The physical model: the R/L feed probes are mounted on the antenna and rotate with it,
    while the incoming field has fixed global H/V polarization. Rotating the antenna by angle
    :math:`\\theta` therefore acts in the feed (output) space of the Jones matrix:

    .. math::
        J_{\\text{eff}} = R^T(\\theta) \\cdot J_{\\text{base}}

    Inverting to obtain the R/L → H/V mapping:

    .. math::
        J_{\\text{eff}}^{-1} = J_{\\text{base}}^{-1} \\cdot R(\\theta)

    where :math:`R(\\theta)` is the 2D rotation matrix for the antenna's orientation and
    :math:`J_{\\text{base}}` is the base Jones matrix (H/V to R/L conversion for the default orientation).

    :param antenna_orientations: Array of :class:`AntennaOrientation` values with shape (rows, cols).
    :param base_jones_matrix: The base Jones matrix mapping H/V to R/L for the default (N) orientation.
        If None, uses :data:`constants.ANTENNA_JONES_MATRIX`.

    :return: Array of effective inverse Jones matrices with shape (rows, cols, 2, 2).
        Multiply with R/L feed vector to obtain global H/V: ``H_V = jones[r, c] @ R_L``.
    """
    if base_jones_matrix is None:
        base_jones_matrix = constants.ANTENNA_JONES_MATRIX

    base_jones_inv = np.linalg.inv(base_jones_matrix)

    rows, cols = antenna_orientations.shape
    jones_matrices = np.empty((rows, cols, 2, 2), dtype=base_jones_inv.dtype)

    for r in range(rows):
        for c in range(cols):
            rot = antenna_orientations[r, c].rotation_matrix()
            jones_matrices[r, c] = base_jones_inv @ rot

    return jones_matrices


def get_cable_wavelength(frequencies: np.ndarray, velocity_factors: np.ndarray):
    """
    Returns the wavelength of the provided subcarrier frequencies on a cable with the given velocity factors.

    :param frequencies: The frequencies of the subcarriers, in Hz, NumPy array.
    :param velocity_factors: The velocity factors of the cable, NumPy array.
    :return: The wavelengths of the subcarriers, in meters, NumPy array.
    """
    return constants.SPEED_OF_LIGHT / frequencies[np.newaxis, :] * velocity_factors[:, np.newaxis]


def parse_combined_array_config(config_dict: dict):
    """
    Parse the configuration file for demos that use combined array.

    :param config_dict: The configuration dictionary to parse.
    :return indexing_matrix: The indexing matrix to map the CSI data of the subarrays to the CSI data of the large array.
    :return board_names_hosts: The names of the boards and their hostnames.
    :return cable_lengths: The lengths of the cables connecting the boards.
    :return cable_velocity_factors: The velocity factors of the cables connecting the boards.
    :return n_rows: The number of rows in the array.
    :return n_cols: The number of columns in the array.
    :return antenna_orientations: Array of :class:`AntennaOrientation` values with shape (n_rows, n_cols), indicating the orientation of each antenna's sub-array in the combined array.
    :raises ValueError: If the configuration is invalid (e.g., invalid antenna indices, missing/duplicate antennas, non-contiguous sub-arrays, invalid rotation).
    """
    config = config_dict.copy()

    # Make sure array is rectangular
    n_rows = len(config["array"])
    if n_rows == 0:
        raise ValueError("Array configuration is empty")
    n_cols = len(config["array"][0])
    if n_cols == 0:
        raise ValueError("Array configuration has empty rows")
    for row_idx, row in enumerate(config["array"]):
        if len(row) != n_cols:
            raise ValueError(f"Array row {row_idx} has {len(row)} columns, expected {n_cols}")

    # Collect list of boards and their hosts
    board_names_hosts = dict()
    for boardname in config["boards"].keys():
        board_names_hosts[boardname] = config["boards"][boardname]["host"]

    # Parse all antenna references and collect per-board mappings:
    # (board_row, board_col) -> (combined_row, combined_col)
    board_antenna_positions = {boardname: dict() for boardname in board_names_hosts.keys()}
    board_orientations = dict()

    for row in range(n_rows):
        for col in range(n_cols):
            entry = config["array"][row][col]
            parts = entry.split(".")
            if len(parts) != 3:
                raise ValueError(f"Invalid antenna reference '{entry}' at row {row}, col {col}. Expected format 'boardname.row.col'")

            name, index_row_str, index_col_str = parts

            if name not in board_names_hosts:
                raise ValueError(f"Unknown board '{name}' referenced at row {row}, col {col}. Available boards: {list(board_names_hosts.keys())}")

            try:
                index_row = int(index_row_str)
                index_col = int(index_col_str)
            except ValueError:
                raise ValueError(f"Non-integer antenna index in reference '{entry}' at row {row}, col {col}")

            if not (0 <= index_row < constants.ROWS_PER_BOARD) or not (0 <= index_col < constants.ANTENNAS_PER_ROW):
                raise ValueError(f"Antenna index out of range in reference '{entry}' at row {row}, col {col}. " f"Expected row in [0, {constants.ROWS_PER_BOARD - 1}], col in [0, {constants.ANTENNAS_PER_ROW - 1}]")

            antenna_id = (index_row, index_col)
            if antenna_id in board_antenna_positions[name]:
                raise ValueError(f"Antenna '{entry}' is used multiple times in the array configuration")
            board_antenna_positions[name][antenna_id] = (row, col)

    # Validate that each board's antennas form a valid, contiguous sub-array:
    # The mapping (board_row, board_col) -> (combined_row, combined_col) must be
    # an affine transformation, i.e., all antennas must be present and adjacent.
    expected_antennas = {(r, c) for r in range(constants.ROWS_PER_BOARD) for c in range(constants.ANTENNAS_PER_ROW)}
    for boardname, positions in board_antenna_positions.items():
        if set(positions.keys()) != expected_antennas:
            missing = expected_antennas - set(positions.keys())
            missing_refs = [f"{boardname}.{r}.{c}" for r, c in sorted(missing)]
            raise ValueError(f"Not all antennas from board '{boardname}' are used. Missing: {missing_refs}")

        # Use antenna (0,0) as origin and compute strides from (1,0) and (0,1)
        origin = np.array(positions[(0, 0)])
        stride_board_row = np.array(positions[(1, 0)]) - origin
        stride_board_col = np.array(positions[(0, 1)]) - origin

        # Match stride vectors to a known orientation (convert to plain int for enum matching)
        stride_tuple = (tuple(int(x) for x in stride_board_row), tuple(int(x) for x in stride_board_col))
        try:
            orientation = AntennaOrientation(stride_tuple)
        except ValueError:
            raise ValueError(f"Board '{boardname}' has an invalid rotation (stride_row={stride_tuple[0]}, stride_col={stride_tuple[1]}). " f"Only 0°/90°/180°/270° rotations are supported, no flips.")

        for (br, bc), (cr, cc) in positions.items():
            expected_pos = origin + br * stride_board_row + bc * stride_board_col
            if not np.array_equal(expected_pos, [cr, cc]):
                raise ValueError(f"Antennas of board '{boardname}' do not form a contiguous sub-array. " f"Antenna {boardname}.{br}.{bc} is at combined array position ({cr}, {cc}), " f"but expected ({expected_pos[0]}, {expected_pos[1]})")

        board_orientations[boardname] = orientation

    # Build the indexing matrix and antenna orientation array from the validated positions
    indexing_matrix = np.zeros((n_rows, n_cols), dtype=int)
    antenna_orientations = np.empty((n_rows, n_cols), dtype=object)
    for boardname, positions in board_antenna_positions.items():
        for (index_row, index_col), (row, col) in positions.items():
            offset_board = list(board_names_hosts.keys()).index(boardname) * constants.ANTENNAS_PER_BOARD
            offset_row = index_row * constants.ANTENNAS_PER_ROW
            indexing_matrix[row, col] = offset_board + offset_row + index_col
            antenna_orientations[row, col] = board_orientations[boardname]

    # Get cable lengths and velocity factors
    cable_lengths = np.asarray([board["cable"]["length"] for board in config["boards"].values()])
    cable_velocity_factors = np.asarray([board["cable"]["velocity_factor"] for board in config["boards"].values()])

    return (
        indexing_matrix,
        board_names_hosts,
        cable_lengths,
        cable_velocity_factors,
        n_rows,
        n_cols,
        antenna_orientations,
    )


def build_combined_array_data(indexing_matrix, input_data):
    """
    Helper for combined array setups. Re-structures data from multiple subarrays into a single large array, using the provided indexing matrix.
    Typically, the input data is the CSI data of the subarrays, but it can also be anything else with shape (datapoints, boards, rows, columns, ...).

    :param indexing_matrix: The indexing matrix to map the CSI data of the subarrays to the CSI data of the large array.
    :param input_data: The data of the subarrays. Complex-valued NumPy array with shape (datapoints, boards, rows, columns, ...).

    :return: The combined array data. Complex-valued NumPy array with shape (datapoints, rows, columns, subcarriers).
    """
    # input_data has shape (datapoint, board, row, column, subcarrier)
    data_by_array_row_col = np.moveaxis(input_data, 0, -1)
    data_by_antenna = np.reshape(
        data_by_array_row_col,
        (data_by_array_row_col.shape[0] * data_by_array_row_col.shape[1] * data_by_array_row_col.shape[2],) + data_by_array_row_col.shape[3:],
    )
    combined_data = data_by_antenna[indexing_matrix]
    combined_data = np.moveaxis(combined_data, -1, 0)

    return combined_data
