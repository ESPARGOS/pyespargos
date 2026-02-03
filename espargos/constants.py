#!/usr/bin/env python3

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
