#!/usr/bin/env python3

import PyQt6.QtWidgets
import PyQt6.QtCore
import PyQt6.QtQml

import espargos.util

import threading
import argparse
import yaml
import enum

from .backlog_settings import BacklogSettings, ConfigManager
from .config_manager import deep_update
from .pool_drawer import PoolDrawer

class ESPARGOSApplicationFlags(enum.Enum):
    ENABLE_BACKLOG = enum.auto()
    COMBINED_ARRAY = enum.auto()
    SINGLE_PREAMBLE_FORMAT = enum.auto()

class ESPARGOSApplication(PyQt6.QtWidgets.QApplication):
    BASE_DEFAULT_CONFIG = {
        "backlog": {
            "size": 20,
            "fields": {
                "lltf": True,
                "ht20": False,
                "ht40": False
            }
        },
        "pool": {
            # Pool configuration settings, handled by PoolDrawer
        },
        "combined-array": {
            # Combined array configuration settings
        },
        "generic": {
            # Generic application settings, handled by GenericAppSettings
            "preamble_format": "lltf"
        },
        "app": {
            # The 'app' section is reserved for application-specific settings
        }
    }

    initComplete = PyQt6.QtCore.pyqtSignal()

    def __init__(self, argv : list[str], argparse_parent : argparse.ArgumentParser = None, flags : set[ESPARGOSApplicationFlags] = set()):
        super().__init__(argv)

        # Basic app initialization
        self.flags = flags
        self.ready = False
        self.engine = PyQt6.QtQml.QQmlApplicationEngine()
        self.aboutToQuit.connect(self.onAboutToQuit)
        self._qml_ok = True

        # Parse command-line arguments
        parser = argparse.ArgumentParser(parents = [argparse_parent] if argparse_parent else [])
        parser.add_argument("-c", "--config", type = str, default = None, help = "Path to YAML configuration file to load")

        if ESPARGOSApplicationFlags.ENABLE_BACKLOG in self.flags:
            parser.add_argument("-b", "--backlog-size", type = int, default = 20, help = "Size of CSI datapoint backlog")

        if ESPARGOSApplicationFlags.SINGLE_PREAMBLE_FORMAT in self.flags:
            format_group = parser.add_mutually_exclusive_group()
            format_group.add_argument("--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
            format_group.add_argument("--ht40", default = False, help = "Use only CSI from HT40", action = "store_true")
            format_group.add_argument("--ht20", default = False, help = "Use only CSI from HT20", action = "store_true")

        self.args = parser.parse_args()

        # Load initial configuration if provided
        self.config_path = None
        self.initial_config = self.BASE_DEFAULT_CONFIG.copy()
        if hasattr(self, "DEFAULT_CONFIG"):
            self.initial_config["app"] = self.DEFAULT_CONFIG.copy()
        if self.args.config is not None:
            with open(self.args.config, "r") as config_file:
                cfg_from_file = yaml.safe_load(config_file)

                if not isinstance(cfg_from_file, dict):
                    raise ValueError("Config file must contain a YAML object at the root")

                deep_update(self.initial_config, cfg_from_file)

        # Override initial config with command-line arguments
        if ESPARGOSApplicationFlags.ENABLE_BACKLOG in self.flags:
            self.initial_config["backlog"]["size"] = self.args.backlog_size

        if ESPARGOSApplicationFlags.SINGLE_PREAMBLE_FORMAT in self.flags:
            # Make sure that only one format is selected
            selected_formats = []
            if self.args.lltf:
                selected_formats.append("lltf")
            if self.args.ht40:
                selected_formats.append("ht40")
            if self.args.ht20:
                selected_formats.append("ht20")
            if len(selected_formats) > 1:
                raise ValueError("At most one of --lltf, --ht40 or --ht20 can be selected!")

            # Remember choice in generic app config
            if len(selected_formats) == 1:
                self.initial_config["generic"]["preamble_format"] = selected_formats[0]

                # *If* a command line flag about preamble format is provided, it also overrides backlog config
                if ESPARGOSApplicationFlags.ENABLE_BACKLOG in self.flags:
                    self.initial_config["backlog"]["fields"] = {
                        "lltf": self.args.lltf,
                        "ht20": self.args.ht20,
                        "ht40": self.args.ht40
                    }

        # Configuration managers
        if ESPARGOSApplicationFlags.ENABLE_BACKLOG in self.flags:
            self.backlog_settings = BacklogSettings(force_config=self.get_initial_config("backlog"), parent=self)
        self.genericconfig = ConfigManager(self.get_initial_config("generic"), parent=self)

    def get_initial_config(self, *keys, default=None):
        for key in keys:
            if key in self.initial_config and isinstance(self.initial_config[key], dict):
                return self.initial_config[key]
        return default

    def init_qml(self, qml_file, context_props: dict | None = None):
        context = self.engine.rootContext()

        # Backlog config manager
        if ESPARGOSApplicationFlags.ENABLE_BACKLOG in self.flags:
            context.setContextProperty("backlogconfig", self.backlog_settings.configManager())

        # Generic app config manager
        context.setContextProperty("genericconfig", self.genericconfig)

        # No callback for generic config changes yet, just mark as handled
        self.genericconfig.updateAppState.connect(self.genericconfig.updateAppStateHandled)

        # Provide backend and optional additional context properties
        context.setContextProperty("backend", self)
        if hasattr(self, "pooldrawer"):
            context.setContextProperty("poolconfig", self.pooldrawer.configManager())            

        for key, value in (context_props or {}).items():
            if key != "backend":
                context.setContextProperty(key, value)

        qml_url = qml_file.as_uri() if hasattr(qml_file, "as_uri") else qml_file
        self.engine.load(qml_url)

    def initialize_pool(
            self,
            hosts : list[str],
            enable_backlog : bool = False,
            backlog_cb_predicate = None,
            additional_calibrate_args : dict = {},
            calibrate : bool = True):
        """
        Initialize ESPARGOS Pool for combined-array setups.
        Also triggers creation of pool drawer.

        enable_backlog: If True, also set up CSI backlog after pool initialization.
        """
        # Setup ESPARGOS pool for combined array setup and calibrate
        self.pool = espargos.Pool([espargos.Board(host) for host in hosts])

        # Pool configuration UI
        pool_cfg = self.get_initial_config("pool", default = {})
        self.pooldrawer = PoolDrawer(self.pool, pool_cfg, parent = self)

        # Wait for config to be applied before starting pool and calibration
        def config_applied():
            def _init_worker():
                self.pool.start()
                if calibrate:
                    self.pool.calibrate(duration = 2, per_board = False, **additional_calibrate_args)

                if enable_backlog:
                    # Do not enable any preamble formats for now, will be set later based on config
                    self.backlog = espargos.CSIBacklog(self.pool, cb_predicate = backlog_cb_predicate, calibrate = calibrate)
                    self.backlog.start()

                    self.backlog_settings.set_backlog(self.backlog)

                self.ready = True
                self.initComplete.emit()

            threading.Thread(target=_init_worker, daemon=True).start()

        self.pooldrawer.initComplete.connect(config_applied)

    # Helper functions for common initialization tasks
    def initialize_combined_array(self, **kwargs):
        """
        Initialize ESPARGOS Pool for combined-array setups.
        Also triggers creation of pool drawer.

        backlog: If True, also set up CSI backlog after pool initialization.
        """
        # Ensure that initial config is not empty
        if not self.initial_config:
            raise ValueError("Configuration for combined-array setups must be provided for combined-array setups!")

        # Load config file
        self.indexing_matrix, self.board_names_hosts, self.cable_lengths, self.cable_velocity_factors, self.n_rows, self.n_cols = espargos.util.parse_combined_array_config(self.get_initial_config("combined-array"))

        # Set up ESPARGOS pool and backlog
        self.initialize_pool(list(self.board_names_hosts.values()), **kwargs, additional_calibrate_args = {
            "cable_lengths": self.cable_lengths,
            "cable_velocity_factors": self.cable_velocity_factors
        })

    def onAboutToQuit(self):
        self.pool.stop()
        if hasattr(self, "backlog"):
            self.backlog.stop()
        if hasattr(self, "engine"):
            self.engine.deleteLater()

    def exec(self):
        if not self._qml_ok:
            return -1
        return super().exec()

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=initComplete)
    def initializing(self):
        return not self.ready

    @PyQt6.QtCore.pyqtProperty(object, constant=False, notify=initComplete)
    def hasBacklog(self):
        return hasattr(self, "backlog")