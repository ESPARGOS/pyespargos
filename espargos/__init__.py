#!/usr/bin/env python

from .calibration import CSICalibration
from .exithandler import ExitHandler
from .backlog import CSIBacklog
from .cluster import CSICluster
from .board import Board, EspargosAPIVersionError, EspargosCsiStreamConnectionError, EspargosHTTPStatusError, EspargosUnexpectedResponseError
from .pool import Pool
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
