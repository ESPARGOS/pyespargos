#!/usr/bin/env python

__all__ = [
    "ANTENNAS_PER_BOARD",
    "ANTENNAS_PER_ROW",
    "ANTENNA_SEPARATION",
    "FFT_GAIN_DB_PER_UNIT",
    "ROWS_PER_BOARD",
    "RX_GAIN_DB_PER_UNIT",
    "SPEED_OF_LIGHT",
    "WIFI_CHANNEL1_FREQUENCY",
    "WIFI_CHANNEL_SPACING",
    "WIFI_SUBCARRIER_SPACING",
]

ANTENNAS_PER_ROW = 4
"Number of antennas per row / per SPI controller on the board"

ROWS_PER_BOARD = 2
"Number of rows / SPI controllers on the board"

SPEED_OF_LIGHT = 299792458
"Speed of light in a vacuum"

ANTENNAS_PER_BOARD = ANTENNAS_PER_ROW * ROWS_PER_BOARD
"Number of antennas on one board"

RX_GAIN_DB_PER_UNIT = 1.0
"Reported RX gain step size in dB"

FFT_GAIN_DB_PER_UNIT = 0.25
"Reported FFT gain step size in dB"

ANTENNA_SEPARATION = 0.06
"Distance between the centers of two antennas [m]"

WIFI_CHANNEL1_FREQUENCY = 2.412e9
"Frequency of channel 1 in 2.4 GHz WiFi"

WIFI_CHANNEL_SPACING = 5e6
"Frequency spacing of WiFi channels"

WIFI_SUBCARRIER_SPACING = 312.5e3
"Subcarrier spacing of WiFi (in Hz)"
