#!/usr/bin/env python3

import PyQt6.QtWidgets
import PyQt6.QtCore
import PyQt6.QtQml

import espargos.util

import numpy as np
import threading
import argparse
import yaml

from .backlog_settings import BacklogSettings, ConfigManager
from .config_manager import deep_update
from .pool_drawer import PoolDrawer


class ESPARGOSApplication(PyQt6.QtWidgets.QApplication):
    """
    Base class for ESPARGOS demo applications.

    This class provides core functionality for ESPARGOS demos including:
    - Command-line argument parsing (config file support)
    - YAML configuration loading
    - QML engine initialization
    - Pool initialization and management

    Use mixins to extend functionality:
    - BacklogMixin: Adds CSI backlog support
    - CombinedArrayMixin: Adds combined array configuration support
    - SingleCSIFormatMixin: Adds single preamble format selection
    """

    BASE_DEFAULT_CONFIG = {
        "backlog": {"size": 20, "fields": {"lltf": True, "ht20": False, "ht40": False}},
        "pool": {
            # Pool configuration settings, partially handled by PoolDrawer
            "hosts": []
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
        },
    }

    initComplete = PyQt6.QtCore.pyqtSignal()

    def __init__(
        self,
        argv: list[str],
        argparse_parent: argparse.ArgumentParser = None,
    ):
        super().__init__(argv)

        # Basic app initialization
        self.ready = False
        self.engine = PyQt6.QtQml.QQmlApplicationEngine()
        self.aboutToQuit.connect(self.onAboutToQuit)
        self._qml_ok = True

        # Parse command-line arguments
        parser = argparse.ArgumentParser(parents=[argparse_parent] if argparse_parent else [])
        parser.add_argument(
            "-c",
            "--config",
            type=str,
            default=None,
            help="Path to YAML configuration file to load",
        )

        # Let mixins add their arguments
        self._add_argparse_arguments(parser)

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

        # Let mixins process their arguments and update config
        self._process_args()

        # Configuration managers
        self._init_config_managers()

        self.genericconfig = ConfigManager(self.get_initial_config("generic"), parent=self)

    def _add_argparse_arguments(self, parser):
        """
        Hook for mixins to add command-line arguments.
        Override in mixins and call super()._add_argparse_arguments(parser).
        """
        # Base class adds hosts argument by default (non-combined-array mode)
        if not self._uses_combined_array():
            parser.add_argument(
                "hosts",
                type=str,
                default="",
                help="Comma-separated list of host addresses (IP or hostname) of ESPARGOS devices",
            )

    def _uses_combined_array(self):
        """Check if this class uses the CombinedArrayMixin."""
        return isinstance(self, CombinedArrayMixin)

    def _uses_backlog(self):
        """Check if this class uses the BacklogMixin."""
        return isinstance(self, BacklogMixin)

    def _uses_single_csi_format(self):
        """Check if this class uses the SingleCSIFormatMixin."""
        return isinstance(self, SingleCSIFormatMixin)

    def _process_args(self):
        """
        Hook for mixins to process command-line arguments.
        Override in mixins and call super()._process_args().
        """
        # If hosts are provided on command line, override pool config
        if hasattr(self.args, "hosts") and len(self.args.hosts) > 0:
            hosts = self.args.hosts.split(",")
            self.initial_config["pool"]["hosts"] = hosts

    def _init_config_managers(self):
        """
        Hook for mixins to initialize their config managers.
        Override in mixins and call super()._init_config_managers().
        """
        pass

    def get_initial_config(self, *path, default=None):
        """
        Retrieve initial configuration value for given path.

        Examples:
            * self.get_initial_config("pool", "hosts", default=[])
            * self.get_initial_config("combined-array")

        Path can be either a sequence of keys, or a single key.
        """
        cfg = self.initial_config
        for key in path if isinstance(path, tuple) else (path,):
            if key in cfg:
                cfg = cfg[key]
            else:
                return default

        return cfg

    def initialize_qml(self, qml_file, context_props: dict | None = None):
        context = self.engine.rootContext()

        # Let mixins set up their context properties
        self._setup_qml_context(context)

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

    def _setup_qml_context(self, context):
        """
        Hook for mixins to set up QML context properties.
        Override in mixins and call super()._setup_qml_context(context).
        """
        pass

    def initialize_pool(
        self,
        backlog_cb_predicate=None,
        additional_calibrate_args: dict = {},
        calibrate: bool = True,
    ):
        """
        Initialize ESPARGOS Pool. Also triggers creation of pool drawer backend.
        """
        # Let mixins prepare pool initialization
        additional_calibrate_args = self._prepare_pool_init(additional_calibrate_args)

        self.pool = espargos.Pool([espargos.Board(host) for host in self.get_initial_config("pool", "hosts")])

        # Pool configuration UI
        pool_cfg = self.get_initial_config("pool")
        self.pooldrawer = PoolDrawer(self.pool, pool_cfg, parent=self)

        # Wait for config to be applied before starting pool and calibration
        def config_applied():
            def _init_worker():
                self.pool.start()
                if calibrate:
                    self.pool.calibrate(duration=2, per_board=False, **additional_calibrate_args)

                # Let mixins finalize pool initialization
                self._finalize_pool_init(backlog_cb_predicate, calibrate)

                self.ready = True
                self.initComplete.emit()

            threading.Thread(target=_init_worker, daemon=True).start()

        self.pooldrawer.initComplete.connect(config_applied)

    def _prepare_pool_init(self, additional_calibrate_args):
        """
        Hook for mixins to prepare pool initialization.
        Override in mixins and call super()._prepare_pool_init(additional_calibrate_args).
        Returns the (possibly modified) additional_calibrate_args.
        """
        return additional_calibrate_args

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        """
        Hook for mixins to finalize pool initialization.
        Override in mixins and call super()._finalize_pool_init(backlog_cb_predicate, calibrate).
        """
        pass

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


class BacklogMixin:
    """
    Mixin that adds CSI backlog support to an ESPARGOSApplication.

    Provides:
    - Backlog size command-line argument (-b/--backlog-size)
    - BacklogSettings configuration manager
    - Automatic backlog creation during pool initialization
    """

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        parser.add_argument(
            "-b",
            "--backlog-size",
            type=int,
            default=20,
            help="Size of CSI datapoint backlog",
        )

    def _process_args(self):
        super()._process_args()
        self.initial_config["backlog"]["size"] = self.args.backlog_size

    def _init_config_managers(self):
        super()._init_config_managers()
        self.backlog_settings = BacklogSettings(force_config=self.get_initial_config("backlog"), parent=self)

    def _setup_qml_context(self, context):
        super()._setup_qml_context(context)
        context.setContextProperty("backlogconfig", self.backlog_settings.configManager())

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)
        # Do not enable any preamble formats for now, will be set later based on config
        self.backlog = espargos.CSIBacklog(
            self.pool,
            cb_predicate=backlog_cb_predicate,
            calibrate=calibrate,
        )
        self.backlog.start()

        self.backlog_settings.set_backlog(self.backlog)


class CombinedArrayMixin:
    """
    Mixin that adds combined array configuration support to an ESPARGOSApplication.

    Provides:
    - Single-array command-line argument (-s/--single-array)
    - Combined array configuration parsing
    - Automatic host extraction from combined array config
    - Cable length calibration support
    """

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        parser.add_argument(
            "-s",
            "--single-array",
            type=str,
            default="",
            help="Array consists of a single ESPARGOS device in horizontal orientation, specify host address (IP or hostname) of device",
        )

    def _process_args(self):
        super()._process_args()
        # In single-array mode, auto-generate combined array config
        if hasattr(self.args, "single_array") and len(self.args.single_array) > 0:
            host = self.args.single_array

            self.initial_config["combined-array"] = {
                "boards": {
                    "arr": {
                        "host": host,
                        "cable": {
                            "length": 0.0,
                            "velocity_factor": 1.0,
                        },
                    }
                },
                "array": [
                    ["arr.0.0", "arr.0.1", "arr.0.2", "arr.0.3"],
                    ["arr.1.0", "arr.1.1", "arr.1.2", "arr.1.3"],
                ],
            }

    def _prepare_pool_init(self, additional_calibrate_args):
        additional_calibrate_args = super()._prepare_pool_init(additional_calibrate_args)

        combined_array_cfg = self.get_initial_config("combined-array")

        # Check if combined_array_cfg was provided (not just empty dictionary)
        if combined_array_cfg is None or combined_array_cfg == {}:
            raise ValueError("Combined array configuration is required for applications using CombinedArrayMixin. You must either provide it via config file or use the --single-array option.")

        (
            self.indexing_matrix,
            hosts,
            cable_lengths,
            cable_velocity_factors,
            self.n_rows,
            self.n_cols,
        ) = espargos.util.parse_combined_array_config(combined_array_cfg)

        additional_calibrate_args["cable_lengths"] = cable_lengths
        additional_calibrate_args["cable_velocity_factors"] = cable_velocity_factors
        self.initial_config["pool"]["hosts"] = list(hosts.values())

        return additional_calibrate_args


class SingleCSIFormatMixin:
    """
    Mixin that adds single preamble format selection to an ESPARGOSApplication.

    Provides:
    - Mutually exclusive command-line arguments (--lltf, --ht20, --ht40)
    - Automatic backlog field configuration when combined with BacklogMixin
    """

    def get_backlog_csi(self, *additional_keys: str) -> np.ndarray | tuple[np.ndarray, ...] | None:
        """
        Retrieve latest CSI datapoints from backlog for the selected preamble format.

        Returns None if backlog does not exist (yet), is empty, or data is otherwise unavailable.
        Automatically interpolates gaps for HT20/HT40 formats and filters out NaN values.

        :param additional_keys: Additional backlog keys to retrieve alongside the CSI data.
        :return: CSI array if no additional keys, tuple of (csi, *additional) if keys specified, or None if unavailable.
        """
        if not hasattr(self, "backlog") or not self.backlog.nonempty():
            return None

        csi_key = self.genericconfig.get("preamble_format")

        try:
            results = self.backlog.get_multiple([csi_key, *additional_keys])
        except ValueError:
            print(f"Requested CSI key {csi_key} not in backlog")
            return None

        csi_backlog = results[0]

        # Interpolate DC subcarrier gap for HT formats
        if csi_key == "ht40":
            espargos.util.interpolate_ht40ltf_gap(csi_backlog)
        elif csi_key == "ht20":
            espargos.util.interpolate_ht20ltf_gap(csi_backlog)

        # Reject data containing NaN values
        if np.any(np.isnan(csi_backlog)):
            return None

        if additional_keys:
            return tuple(results)
        return csi_backlog

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        format_group = parser.add_mutually_exclusive_group()
        format_group.add_argument(
            "--lltf",
            default=False,
            help="Use only CSI from L-LTF (set up backlog and application accordingly)",
            action="store_true",
        )
        format_group.add_argument(
            "--ht40",
            default=False,
            help="Use only CSI from HT40 (set up backlog and application accordingly)",
            action="store_true",
        )
        format_group.add_argument(
            "--ht20",
            default=False,
            help="Use only CSI from HT20 (set up backlog and application accordingly)",
            action="store_true",
        )

    def _process_args(self):
        super()._process_args()

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
            if self._uses_backlog():
                self.initial_config["backlog"]["fields"] = {
                    "lltf": self.args.lltf,
                    "ht20": self.args.ht20,
                    "ht40": self.args.ht40,
                }
