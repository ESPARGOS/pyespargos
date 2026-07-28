#!/usr/bin/env python

from .calibration import CSICalibration
from .backlog import BacklogFilter, CSIBacklog, Exclude11bFilter, MacFilter
from .csi_cluster import CSICluster
from .board import Board, BoardCapability, BoardControl, EspargosAPIVersionError, EspargosHTTPStatusError, EspargosStreamConnectionError, EspargosUnexpectedResponseError, SensorMessageSubscription
from .board_wifi_rx import WiFiRx
from .board_wifi_tx import WiFiTx

Board.register_capability("wifi_rx", WiFiRx)
Board.register_capability("wifi_tx", WiFiTx)

from .pool import Pool
from .csi_pool import CSIPool, CalibrationError
from .sensor_cluster import ClusterCollisionError, SensorCluster
from .csi_packet import CSIPacket
from .radar_packet import RadarTxReportPacket
from .sensor import RFSwitchState, SensorFragment, SensorMessage, SensorPacket
from .wifi import SequenceControl, WiFiFrameKey, WiFiPhyMode, WiFiPhyRate, WiFiTxPower
from . import csi_compression
from . import csi_packet
from . import csi_cluster
from . import csi_pool
from . import radar
from . import radar_packet
from . import sensor
from . import sensor_cluster
from . import util
from . import wifi
from . import board_wifi_rx
from . import board_wifi_tx
from .radar import RadarPoolConfig
import logging
import sys

__version__ = "0.1.1"
__title__ = "pyespargos"
__description__ = "Python library for working with the ESPARGOS WiFi channel sounder"
__uri__ = "http://github.com/ESPARGOS/pyespargos"


class _ColorFormatter(logging.Formatter):
    """Formatter that adds ANSI colors based on log level."""

    RESET = "\033[0m"
    COLORS = {
        logging.DEBUG: "\033[37m",  # white
        logging.INFO: "\033[32m",  # green
        logging.WARNING: "\033[33m",  # yellow
        logging.ERROR: "\033[31m",  # red
        logging.CRITICAL: "\033[31m",  # red
    }

    def __init__(self, fmt=None, **kwargs):
        super().__init__(fmt, **kwargs)

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        msg = super().format(record)
        return f"{color}{msg}{self.RESET}"


class Logger:
    """
    Logger class for pyespargos. This class is a singleton and should be used to modify the logging level of the library.
    """

    logger = logging.getLogger("pyespargos")
    stderrHandler = logging.StreamHandler(sys.stderr)
    stderrHandler.setFormatter(_ColorFormatter("[%(name)-20s] %(message)s"))
    logger.addHandler(stderrHandler)
    logger.setLevel(level=logging.INFO)

    @classmethod
    def get_level(cls):
        """
        Returns the current logging level of the logger.
        """
        return cls.logger.getEffectiveLevel()

    @classmethod
    def set_level(cls, level):
        """
        Sets the logging level of the logger.
        """
        cls.logger.setLevel(level=level)
