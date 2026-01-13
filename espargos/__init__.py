#!/usr/bin/env python3

from .calibration import CSICalibration
from .exithandler import ExitHandler
from .backlog import CSIBacklog
from .cluster import CSICluster
from .board import Board
from .pool import Pool
import logging
import sys

__version__ = "0.1.1"
__title__ = "pyespargos"
__description__ = "Python library for working with the ESPARGOS WiFi channel sounder"
__uri__ = "http://github.com/ESPARGOS/pyespargos"

class Logger:
    """
    Logger class for pyespargos. This class is a singleton and should be used to modify the logging level of the library.
    """
    logger = logging.getLogger("pyespargos")
    stderrHandler = logging.StreamHandler(sys.stderr)
    stderrHandler.setFormatter(logging.Formatter("[%(name)-20s] %(message)s"))
    logger.addHandler(stderrHandler)
    logger.setLevel(level = logging.INFO)

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
