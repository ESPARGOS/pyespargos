#!/usr/bin/env python

from .csi_calibration import CSICalibration
from .sensor_calibration import ClockReferenceScope, SensorCalibration
from .csi_backlog import CSIBacklog, CSIBacklogFilter, Exclude11bFilter, MacFilter
from .sensor_backlog import BacklogField, BacklogFilter, SensorBacklog
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
from . import csi_backlog
from . import csi_packet
from . import csi_cluster
from . import csi_pool
from . import radar
from . import radar_packet
from . import sensor
from . import sensor_backlog
from . import sensor_cluster
from . import array_processing
from . import combined_array
from . import csi_processing
from . import delay_estimation
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


def _load_addons():
    """
    Load ESPARGOS addon packages.

    Addons extend pyespargos on import, e.g. by registering additional Board
    capabilities or application modalities. Two discovery mechanisms are
    supported:

    - Addon checkouts in the ``addons/`` directory of a pyespargos repository
      checkout: every package named ``espargos_*`` inside a subdirectory of
      ``addons/`` is imported automatically.
    - Installed addon packages that declare an entry point in the
      ``espargos.addons`` group.

    A failing addon import is logged but never prevents pyespargos itself
    from loading. Safe to call more than once; already-imported addons are
    skipped.
    """
    import importlib
    import importlib.metadata
    import pathlib

    addons_dir = pathlib.Path(__file__).resolve().parents[1] / "addons"
    if addons_dir.is_dir():
        for package_init in sorted(addons_dir.glob("*/espargos_*/__init__.py")):
            package_name = package_init.parent.name
            if package_name in sys.modules:
                continue
            checkout_dir = str(package_init.parents[1])
            if checkout_dir not in sys.path:
                sys.path.insert(0, checkout_dir)
            try:
                importlib.import_module(package_name)
                Logger.logger.info(f"Loaded ESPARGOS addon {package_name!r} from {checkout_dir}")
            except Exception:
                Logger.logger.exception(f"Failed to load ESPARGOS addon {package_name!r}")

    try:
        addon_entry_points = importlib.metadata.entry_points(group="espargos.addons")
    except Exception:
        addon_entry_points = ()
    for entry_point in addon_entry_points:
        if entry_point.value.partition(":")[0] in sys.modules:
            continue
        try:
            entry_point.load()
            Logger.logger.info(f"Loaded ESPARGOS addon {entry_point.name!r}")
        except Exception:
            Logger.logger.exception(f"Failed to load ESPARGOS addon {entry_point.name!r}")


_load_addons()
