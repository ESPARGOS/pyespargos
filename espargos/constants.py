#!/usr/bin/env python3

import numpy as np

ANTENNAS_PER_ROW = 4
"Number of antennas per row / per SPI controller on the board"

ROWS_PER_BOARD = 2
"Number of rows / SPI controllers on the board"

SPEED_OF_LIGHT = 299792458
"Speed of light in a vacuum"

ANTENNAS_PER_BOARD = ANTENNAS_PER_ROW * ROWS_PER_BOARD
"Number of antennas on one board"

ANTENNA_SEPARATION = 0.06
"Distance between the centers of two antennas [m]"

WIFI_CHANNEL1_FREQUENCY = 2.412e9
"Frequency of channel 1 in 2.4 GHz WiFi"

WIFI_CHANNEL_SPACING = 5e6
"Frequency spacing of WiFi channels"

WIFI_SUBCARRIER_SPACING = 312.5e3
"Subcarrier spacing of WiFi (in Hz)"

ANTENNA_JONES_MATRIX_SIMPLE = np.sqrt(2) / 2 * np.asarray([[1, -1], [1, 1]])
"Simple Jones matrix to convert from linear (H/V) to feed (R/L) polarization basis"

ANTENNA_JONES_CROSSPOL_MATRIX = np.asarray([[0.33, 0.05 + 0.05j], [0.05 + 0.05j, 0.33]])
ANTENNA_JONES_CROSSPOL_MATRIX = ANTENNA_JONES_CROSSPOL_MATRIX / np.linalg.norm(ANTENNA_JONES_CROSSPOL_MATRIX, ord="fro")
"Empirically determined cross-polarization component (due to intentional elliptical antenna polarization) of the Jones matrix"

# Jones matrix is only a rough approximation for now
ANTENNA_JONES_MATRIX = ANTENNA_JONES_MATRIX_SIMPLE @ ANTENNA_JONES_CROSSPOL_MATRIX  # np.sqrt(2) / 2 * (0.8 * np.asarray([[1, -1], [1, 1]]) - 0.1j * np.asarray([[1, 1], [1, -1]]))
"Jones matrix to convert from linear (H/V) to feed (R/L) polarization basis"
