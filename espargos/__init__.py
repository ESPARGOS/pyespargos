#!/usr/bin/env python

from . import board
from . import constants
from . import csi_compression
from . import csi_calibration
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
from . import pool
from . import revisions
from . import sensor_calibration
from . import uart
from . import wifi
from . import board_wifi_rx
from . import board_wifi_tx
import logging
import sys

_PUBLIC_MODULES = (
    array_processing,
    board,
    board_wifi_rx,
    board_wifi_tx,
    combined_array,
    constants,
    csi_backlog,
    csi_calibration,
    csi_cluster,
    csi_compression,
    csi_packet,
    csi_pool,
    csi_processing,
    delay_estimation,
    pool,
    radar,
    radar_packet,
    revisions,
    sensor,
    sensor_backlog,
    sensor_calibration,
    sensor_cluster,
    uart,
    wifi,
)

board.Board.register_capability("wifi_rx", board_wifi_rx.WiFiRxCapability)
board.Board.register_capability("wifi_tx", board_wifi_tx.WiFiTxCapability)

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

    _logger = logging.getLogger("pyespargos")
    _stderr_handler = logging.StreamHandler(sys.stderr)
    _stderr_handler.setFormatter(_ColorFormatter("[%(name)-20s] %(message)s"))
    _logger.addHandler(_stderr_handler)
    _logger.setLevel(level=logging.INFO)

    @classmethod
    def get_level(cls):
        """
        Returns the current logging level of the logger.
        """
        return cls._logger.getEffectiveLevel()

    @classmethod
    def set_level(cls, level):
        """
        Sets the logging level of the logger.
        """
        cls._logger.setLevel(level=level)


def _export_public_module_symbols():
    """Expose every supported module symbol at the package root."""

    owners = {}
    exported_symbols = []
    for module in _PUBLIC_MODULES:
        for name in module.__all__:
            if name in owners:
                raise RuntimeError(f"Public API name {name!r} is exported by both {owners[name]!r} and {module.__name__!r}")
            owners[name] = module.__name__
            globals()[name] = getattr(module, name)
            exported_symbols.append(name)
    return exported_symbols


_PUBLIC_MODULE_NAMES = [module.__name__.rpartition(".")[2] for module in _PUBLIC_MODULES]
__all__ = ["__version__", "Logger", *_PUBLIC_MODULE_NAMES, *_export_public_module_symbols()]


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
                Logger._logger.info(f"Loaded ESPARGOS addon {package_name!r} from {checkout_dir}")
            except Exception:
                Logger._logger.exception(f"Failed to load ESPARGOS addon {package_name!r}")

    try:
        addon_entry_points = importlib.metadata.entry_points(group="espargos.addons")
    except Exception:
        addon_entry_points = ()
    for entry_point in addon_entry_points:
        if entry_point.value.partition(":")[0] in sys.modules:
            continue
        try:
            entry_point.load()
            Logger._logger.info(f"Loaded ESPARGOS addon {entry_point.name!r}")
        except Exception:
            Logger._logger.exception(f"Failed to load ESPARGOS addon {entry_point.name!r}")


_load_addons()
